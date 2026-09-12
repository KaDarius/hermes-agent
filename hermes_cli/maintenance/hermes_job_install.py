"""Exact-target Hermes installation transaction.

This component does not select a host, import Hermes, execute jobs, repair stores,
or retrieve secrets. The caller must validate the accepted source/host, provide
its supported jobs module and a read-only active-execution check. All mutation is
explicit through apply/rollback. This is not a fleet-wide deployment claim.
"""
from __future__ import annotations
import contextlib
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import uuid

JOB_ID='8417d6710834'
FILES=frozenset({'hermes_daily_command_hub.py','daily-command-hub-config.json'})
FIELDS=('script','no_agent','provider_snapshot','model_snapshot')
MAX_STORE=4*1024*1024
PROGRESS=frozenset({'last_run_at','last_status','last_error','last_delivery_error','failure_streak','next_run_at','fire_claim','run_claim'})

class Refused(RuntimeError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _unique(pairs):
    d={}
    for k,v in pairs:
        if k in d:raise Refused('duplicate_json_key')
        d[k]=v
    return d

def _finite_float(value):
    number=float(value)
    if not math.isfinite(number):raise Refused("nonfinite_json_number")
    return number

def _json(raw):
    try:
        return json.loads(raw,object_pairs_hook=_unique,parse_float=_finite_float,parse_constant=lambda _: (_ for _ in ()).throw(Refused('invalid_json_constant')))
    except (ValueError,RecursionError) as e:raise Refused('invalid_json') from None

def _read(path,limit):
    try:
        # Validate the opened type without waiting indefinitely on a FIFO.
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except OSError:raise Refused('unsafe_path') from None
    with os.fdopen(fd,'rb') as f:
        s=os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid():raise Refused('unsafe_path')  # windows-footgun: ok — package import rejects non-POSIX hosts
        raw=f.read(limit+1)
    if len(raw)>limit:raise Refused('oversized_file')
    return raw

def _store(profile):
    raw=_read(profile/'cron/jobs.json',MAX_STORE);d=_json(raw)
    if not isinstance(d,dict) or not {'jobs'}<=set(d)<={'jobs','updated_at'} or not isinstance(d['jobs'],list):raise Refused('invalid_store_shape')
    ids=[]
    for row in d['jobs']:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id']:raise Refused('invalid_job_shape')
        ids.append(row['id'])
    if len(ids)!=len(set(ids)):raise Refused('duplicate_job_id')
    return raw,d['jobs']

def _target(rows):
    matches=[r for r in rows if r['id']==JOB_ID]
    if len(matches)!=1:raise Refused('target_not_unique')
    return matches[0]

def _paths(profile):
    profile=Path(profile)
    for p in [profile,profile/'cron',profile/'scripts']:
        if p.is_symlink() or not p.is_dir() or p.stat().st_uid!=os.getuid():raise Refused('unsafe_path')  # windows-footgun: ok — package import rejects non-POSIX hosts
    return profile

@contextlib.contextmanager
def _file_lock(path):
    fd=None
    try:
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid():raise Refused('unsafe_lock')  # windows-footgun: ok — package import rejects non-POSIX hosts
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise Refused('lock_busy') from None
        yield
    finally:
        if fd is not None:os.close(fd)

class _JobsLock:
    def __init__(self,path):self.path=path;self.depth=0
    @contextlib.contextmanager
    def __call__(self):
        if self.depth:
            self.depth+=1
            try:yield
            finally:self.depth-=1
        else:
            with _file_lock(self.path):
                self.depth=1
                try:yield
                finally:self.depth=0

@contextlib.contextmanager
def _transaction(profile,backend):
    key=f'{(profile/"cron").resolve()}::{JOB_ID}'
    fire=profile/'cron'/f'.fire-{uuid.uuid5(uuid.NAMESPACE_URL,key).hex}.lock'
    lock=_JobsLock(profile/'cron/.jobs.lock')
    old=(backend._jobs_lock,backend.load_jobs,backend.save_jobs)
    # Strict wrappers are scoped to this short-lived installer process. The
    # installed Hermes source and running gateway are never monkeypatched.
    with _file_lock(fire),lock():
        backend._jobs_lock=lock
        backend.load_jobs=lambda:copy.deepcopy(_store(profile)[1])
        try:yield old[2]
        finally:backend._jobs_lock,backend.load_jobs,backend.save_jobs=old

def _idle(row,check_idle):
    if row.get('fire_claim') is not None or row.get('run_claim') is not None:raise Refused('in_flight_claim')
    if row.get('state')!='scheduled' or row.get('enabled') is not True:raise Refused('unexpected_job_state')
    try:nxt=datetime.fromisoformat(row['next_run_at'])
    except (ValueError,KeyError,TypeError):raise Refused('invalid_next_run') from None
    if nxt.tzinfo is None or (nxt-datetime.now(timezone.utc)).total_seconds()<300:raise Refused('run_too_close')
    check_idle()

def _fsync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def install_new(path,data,on_publish=lambda:None):
    """Atomic no-clobber publication; never truncate an existing destination."""
    fd,tmp=tempfile.mkstemp(prefix='.hermes-install-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.link(tmp,path,follow_symlinks=False)
        on_publish()
        _fsync_dir(path.parent)
    finally:os.unlink(tmp)

def _journal(backup,value):
    fd,tmp=tempfile.mkstemp(prefix='.receipt-',dir=backup)
    try:
        with os.fdopen(fd,'w', encoding="utf-8") as f:
            json.dump(value,f,indent=2,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,backup/'transaction.json');_fsync_dir(backup)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _changed_rows(rows,target):
    return [copy.deepcopy(target if r['id']==JOB_ID else r) for r in rows]

def _update(profile,backend,native_save,before_rows,expected_target,updates):
    expected=_changed_rows(before_rows,expected_target)
    initial_raw=_store(profile)[0]
    def guarded_save(rows,*args,**kwargs):
        if args or kwargs or rows!=expected:raise Refused('unexpected_writer_delta')
        if _store(profile)[0]!=initial_raw:raise Refused('store_drift')
        native_save(rows)
    backend.save_jobs=guarded_save
    backend.update_job(JOB_ID,updates)
    _,after=_store(profile)
    _fsync_dir(profile/'cron')
    if after!=expected:raise Refused('write_readback_mismatch')
    return expected

def _owned_files(profile,payloads):
    for name,data in payloads.items():
        p=profile/'scripts'/name
        if _read(p,1024*1024)!=data:raise Refused('installed_file_drift')

def apply(profile,backend,expected_target_hash,payloads,backup,check_idle):
    profile=_paths(profile);backup=Path(backup)
    if set(payloads)!=FILES or any(not isinstance(b,bytes) or not b or len(b)>1024*1024 for b in payloads.values()):raise Refused('invalid_payload')
    with _transaction(profile,backend) as native_save:
        before_raw,rows=_store(profile);before=copy.deepcopy(_target(rows))
        if digest(before)!=expected_target_hash:raise Refused('target_drift')
        _idle(before,check_idle)
        if before.get('name')!='daily-command-hub' or before.get('script') is not None or before.get('no_agent') is not False:raise Refused('unexpected_target_mode')
        if any(k not in before for k in FIELDS) or any(before[k] is not None for k in FIELDS[2:]):raise Refused('unsupported_snapshot_state')
        if not all(isinstance(before.get(k),str) and before[k].strip() and before[k]==before[k].strip() for k in ['provider','model']):raise Refused('unpinned_provider')
        for name in FILES:
            p=profile/'scripts'/name
            if p.exists() or p.is_symlink():raise Refused('destination_exists')
        backup.mkdir(mode=0o700)
        _fsync_dir(backup.parent)
        receipt={'version':1,'profile':str(profile.resolve()),'job_id':JOB_ID,'before':before,'files':{n:{'before':'absent','sha256':hashlib.sha256(b).hexdigest()} for n,b in payloads.items()},'status':'prepared'}
        _journal(backup,receipt)
        installed=[]
        try:
            for name,data in payloads.items():
                install_new(profile/'scripts'/name,data,lambda:installed.append(name))
            _owned_files(profile,payloads)
            updates={'script':'hermes_daily_command_hub.py','no_agent':True}
            expected=dict(before,**updates,provider_snapshot=None,model_snapshot=None)
            # Repeat the active-execution check immediately before the writer.
            _idle(_target(_store(profile)[1]),check_idle)
            _update(profile,backend,native_save,rows,expected,updates)
            receipt.update(status='installed',after=expected);_journal(backup,receipt)
            return {'status':'installed','job_id':JOB_ID,'target_sha256':digest(expected),'backup':str(backup)}
        except BaseException:
            unchanged=False
            try:unchanged=_store(profile)[0]==before_raw
            except BaseException:pass
            if unchanged:
                for name in reversed(installed):
                    p=profile/'scripts'/name
                    if _read(p,1024*1024)!=payloads[name]:raise Refused('installed_file_drift') from None
                    p.unlink()
                receipt['status']='failed_before_job_change';_journal(backup,receipt)
                _fsync_dir(profile/'scripts')
                raise
            receipt['status']='reconciliation_required';_journal(backup,receipt)
            raise Refused('write_outcome_requires_reconciliation') from None

def rollback(profile,backend,backup,check_idle):
    profile=_paths(profile);backup=Path(backup)
    receipt=_json(_read(backup/'transaction.json',MAX_STORE))
    allowed={'installed','rollback_started','job_reverted_files_pending','rolled_back'}
    if not isinstance(receipt,dict) or receipt.get('version')!=1 or receipt.get('status') not in allowed or receipt.get('profile')!=str(profile.resolve()) or receipt.get('job_id')!=JOB_ID:raise Refused('invalid_rollback_receipt')
    if set(receipt.get('files',{}))!=FILES:raise Refused('invalid_rollback_receipt')
    for key in ('before','after'):
        if not isinstance(receipt.get(key),dict) or any(k not in receipt[key] for k in FIELDS):raise Refused('invalid_rollback_receipt')
    if not all(isinstance(receipt['before'].get(k),str) and receipt['before'][k].strip() and receipt['before'][k]==receipt['before'][k].strip() for k in ('provider','model')):raise Refused('unpinned_provider')
    with _transaction(profile,backend) as native_save:
        _,rows=_store(profile);current=_target(rows);_idle(current,check_idle)
        matches=lambda row:all(k in current and current[k]==row[k] for k in FIELDS)
        reverted=matches(receipt['before'])
        if receipt['status']=='installed' and not matches(receipt['after']):raise Refused('target_drift')
        if not reverted and not matches(receipt['after']):raise Refused('target_drift')
        stable=lambda row:{k:v for k,v in row.items() if k not in PROGRESS and k not in FIELDS}
        if stable(current)!=stable(receipt['after']):raise Refused('configuration_drift')
        if receipt['status']=='rolled_back':
            if not reverted:raise Refused('target_drift')
            # A final journal rename may have succeeded before its directory
            # fsync failed. Retry only proves the already reversed state;
            # never remove any path that appeared after completed cleanup.
            if any((profile/'scripts'/n).exists() or (profile/'scripts'/n).is_symlink() for n in FILES):raise Refused('installed_file_drift')
            _fsync_dir(profile/'cron');_fsync_dir(profile/'scripts');_journal(backup,receipt)
            return {'status':'rolled_back','job_id':JOB_ID,'target_sha256':digest(current)}
        for name,meta in receipt['files'].items():
            p=profile/'scripts'/name
            if not isinstance(meta,dict) or meta.get('before')!='absent':raise Refused('invalid_rollback_receipt')
            if reverted and not p.exists() and not p.is_symlink():continue
            if hashlib.sha256(_read(p,1024*1024)).hexdigest()!=meta.get('sha256'):raise Refused('installed_file_drift')
        updates={k:receipt['before'][k] for k in FIELDS};expected=dict(current,**updates)
        if not reverted:
            # Persist intent before any reverse write. A failed native save or
            # receipt publication can then be retried using exact readback.
            receipt['status']='rollback_started';_journal(backup,receipt)
            _update(profile,backend,native_save,rows,expected,updates)
        _fsync_dir(profile/'cron')
        receipt['status']='job_reverted_files_pending';_journal(backup,receipt)
        for name,meta in receipt['files'].items():
            p=profile/'scripts'/name
            if not p.exists() and not p.is_symlink():continue
            if hashlib.sha256(_read(p,1024*1024)).hexdigest()!=meta['sha256']:raise Refused('installed_file_drift')
            p.unlink()
        _fsync_dir(profile/'scripts');receipt['status']='rolled_back';_journal(backup,receipt)
        return {'status':'rolled_back','job_id':JOB_ID,'target_sha256':digest(expected)}


def require_idle_execution_store(profile):
    """Read the existing Hermes execution DB; never initialize/repair its schema."""
    p=Path(profile)/'cron/executions.db'
    if p.is_symlink() or not p.is_file():raise Refused('execution_store_unavailable')
    try:
        conn=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            conn.execute('PRAGMA query_only=ON')
            active=conn.execute("SELECT 1 FROM executions WHERE job_id=? AND status IN ('claimed','running') LIMIT 1",(JOB_ID,)).fetchone()
        finally:conn.close()
    except sqlite3.Error:raise Refused('execution_store_unavailable') from None
    if active:raise Refused('active_execution')
