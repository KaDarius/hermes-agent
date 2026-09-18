"""Isolated drain state machine, NOT a live gateway adapter.

Authority.guard is an opaque, context-bound capability: it must establish and
hold all named admission/writer exclusions for the exact binding and owner,
including queued/manual/internal work. It yields a context-bound require_valid
callable, rechecked before mutations; its successful return must be None. Returning a 'verified' JSON flag cannot
provide this capability. The default live authority always refuses.

Store operations run only inside that capability. create_marker must publish
without clobbering and report an opaque generation token immediately after
publication. remove_owned must compare BOTH generation and bytes and remove
indivisibly under exclusion of EVERY cooperating marker writer. Native Hermes
clear_drain_request is NOT an implementation of that contract. There is no live
store implementation here. Receipt writes must be atomic and durable.

Status must come from a trusted live adapter with fresh per-counter validity
and an acknowledgment of this exact marker. Existing gateway_state.json does
not supply this contract. These types do not manufacture live proof.
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import uuid

from .hermes_job_install import Refused

REQUIRED_ROUTES=frozenset({'gateway_cron','queued_workers','internal_recovery',
    'external_cron','manual_cli','jobs_store_writers','runtime_patchers','drain_marker_writers'})
COUNTERS=frozenset({'messaging','cron','api'})

@dataclass(frozen=True)
class Binding:
    profile: str
    pid: int
    start_time: int
    code_sha: str
    routes: frozenset

    def __post_init__(self):
        if not isinstance(self.profile,str) or not Path(self.profile).is_absolute() or '..' in Path(self.profile).parts:
            raise Refused('invalid_binding')
        if type(self.pid) is not int or self.pid<=0 or type(self.start_time) is not int or self.start_time<=0:
            raise Refused('invalid_binding')
        if not isinstance(self.code_sha,str) or not re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})',self.code_sha):
            raise Refused('invalid_binding')
        if type(self.routes) is not frozenset or self.routes!=REQUIRED_ROUTES:raise Refused('incomplete_route_set')

    def record(self):
        return {'profile':self.profile,'pid':self.pid,'start_time':self.start_time,
                'code_sha':self.code_sha,'routes':sorted(self.routes)}

class LiveAuthority:
    @contextlib.contextmanager
    def guard(self,binding,owner,purpose,deadline):
        raise Refused('live_authority_unavailable')
        yield  # pragma: no cover - preserves the context-manager protocol


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def _require_capability(require_valid):
    if not callable(require_valid):raise Refused('invalid_authority_capability')
    if require_valid() is not None:raise Refused('invalid_authority_capability')


def capture_counters(readers):
    """Call raw counters INSIDE the existing gateway. Never wrap its current
    fail-open count helpers or import a fresh scheduler as a live witness.
    Failures carry no exception text, job/session contents or secret values.
    """
    if not isinstance(readers,dict) or set(readers)!=COUNTERS or any(not callable(v) for v in readers.values()):
        raise Refused('invalid_counter_readers')
    result={}
    for name,reader in readers.items():
        try:
            count=reader()
            valid=type(count) is int and count>=0
        except Exception:
            valid=False;count=None
        result[name]={'valid':valid,'count':count if valid else None}
    return result


def validate_status(snapshot,binding,now,requested_at,marker_sha,*,resuming=False):
    if not isinstance(snapshot,dict):raise Refused('invalid_status')
    expected=binding.record()
    if any(type(snapshot.get(k)) is not type(expected[k]) or snapshot[k]!=expected[k]
           for k in ('profile','pid','start_time','code_sha')):
        raise Refused('process_identity')
    at=snapshot.get('observed_at')
    if not _finite(at) or not _finite(now):raise Refused('invalid_status_time')
    if at>now+1:raise Refused('future_status')
    if at<requested_at or now-at>5:raise Refused('stale_status')
    if snapshot.get('gateway_state')!=('running' if resuming else 'draining'):
        raise Refused('unexpected_gateway_state')
    if snapshot.get('marker_sha256')!=marker_sha or 'marker_sha256' not in snapshot:
        raise Refused('marker_not_acknowledged')
    if resuming:return
    routes=snapshot.get('admission_routes')
    if not isinstance(routes,dict) or set(routes)!=binding.routes or any(v!='held' for v in routes.values()):
        raise Refused('admission_not_held')
    counters=snapshot.get('counters')
    if not isinstance(counters,dict) or set(counters)!=COUNTERS:raise Refused('invalid_counters')
    for item in counters.values():
        if not isinstance(item,dict) or set(item)!={'valid','count'}:raise Refused('invalid_counter')
        if item['valid'] is not True:raise Refused('unreadable_counter')
        if type(item['count']) is not int or item['count']<0:raise Refused('invalid_counter')
        if item['count']:raise Refused('active_work')


class DrainController:
    def __init__(self,binding,authority,store,clock,*,window=600):
        if not _finite(window) or not 0<window<=600:raise Refused('invalid_window')
        self.binding=binding;self.authority=authority;self.store=store;self.clock=clock
        self.begin_completed=False
        self.owner=uuid.uuid4().hex
        self.started=clock.monotonic();self.requested_at=clock.time();self.deadline=self.started+window
        if not _finite(self.started) or not _finite(self.requested_at):raise Refused('invalid_clock')
        self.marker=(json.dumps({'action':'drain','requested_at':datetime.fromtimestamp(self.requested_at,timezone.utc).isoformat(),
            'principal':'codex-hermes-maintenance:'+self.owner,'maintenance_owner':self.owner,
            'epoch':'','suppress_notification':False},sort_keys=True)+'\n').encode()
        self.marker_sha256=hashlib.sha256(self.marker).hexdigest()
        self.receipt={'version':1,'owner':self.owner,'binding':binding.record(),
            'marker_sha256':self.marker_sha256,'requested_at':self.requested_at,
            'state':'new','marker_token':None}

    def _normal_window(self):
        now=self.clock.monotonic()
        if not _finite(now) or now<self.started or now>=self.deadline:raise Refused('window_expired')
        self._wall_progress(self.requested_at,self.started,now)

    def _wall_progress(self,wall_start,mono_start,mono_now):
        wall_now=self.clock.time()
        if (not all(_finite(v) for v in (wall_now,wall_start,mono_start,mono_now))
                or mono_now<mono_start or abs((wall_now-wall_start)-(mono_now-mono_start))>1):
            raise Refused('clock_discontinuity')

    def _save(self,state):
        candidate=dict(self.receipt,state=state)
        self.store.write_receipt(candidate)
        self.receipt=candidate

    def _require_owned(self):
        current=self.store.inspect()
        if current!={'token':self.receipt['marker_token'],'sha256':self.marker_sha256}:
            raise Refused('marker_replaced')

    def begin(self):
        self._normal_window()
        with self.authority.guard(self.binding,self.owner,'begin',self.deadline) as capability:
            _require_capability(capability)
            self._normal_window()
            if self.store.read_receipt() is not None:raise Refused('receipt_exists')
            if self.store.inspect() is not None:raise Refused('marker_exists')
            self._save('prepared')
            _require_capability(capability)
            self._normal_window()
            def published(token):
                if not isinstance(token,str) or not token:raise Refused('invalid_marker_token')
                self.receipt['marker_token']=token
                self._save('drain_requested')
            self.store.create_marker(self.marker,published)
            self._require_owned()
            _require_capability(capability)
            self._normal_window()
            self.begin_completed=True
            return self.receipt.copy()

    def _check_snapshot(self,snapshot,capability):
        _require_capability(capability)
        self._normal_window()
        if not self.begin_completed or self.receipt['state']!='drain_requested':raise Refused('drain_not_requested')
        self._require_owned()
        validate_status(snapshot,self.binding,self.clock.time(),self.requested_at,self.marker_sha256)
        _require_capability(capability)

    def require_ready(self,snapshot):
        self._normal_window()
        with self.authority.guard(self.binding,self.owner,'readiness',self.deadline) as capability:
            self._check_snapshot(snapshot,capability)
        # Instant diagnostic only; use installation_guard around actual work.

    @contextlib.contextmanager
    def installation_guard(self,read_snapshot):
        self._normal_window()
        with self.authority.guard(self.binding,self.owner,'installation',self.deadline) as capability:
            active=True
            def check():
                if not active:raise Refused('installation_context_closed')
                self._check_snapshot(read_snapshot(),capability)
            try:
                check()
                yield check
                _require_capability(capability)
                self._normal_window()
            finally:
                active=False

    def _cleanup_times(self):
        started=self.clock.monotonic()
        if not _finite(started):raise Refused('invalid_clock')
        return started,started+30

    def _check_cleanup_time(self,started,deadline):
        now=self.clock.monotonic()
        if not _finite(now):raise Refused('invalid_clock')
        if now<started or now>=deadline:raise Refused('cleanup_expired')

    def release(self):
        # Expired normal authority cannot be silently extended. This separate
        # capability is narrow: exact-owned-marker cleanup only, no job work.
        cleanup_started,cleanup_deadline=self._cleanup_times()
        with self.authority.guard(self.binding,self.owner,'cleanup',cleanup_deadline) as capability:
            _require_capability(capability)
            self._check_cleanup_time(cleanup_started,cleanup_deadline)
            current=self.store.inspect()
            token=self.receipt.get('marker_token')
            if token is None:
                if current is not None:raise Refused('publication_ambiguous')
                self._save('cancelled_before_publication')
                return
            if current is not None:self._require_owned()
            self._save('release_intent')
            _require_capability(capability)
            self._check_cleanup_time(cleanup_started,cleanup_deadline)
            result=self.store.remove_owned(token,self.marker_sha256)
            if result not in ('removed','absent'):raise Refused('unexpected_remove_result')
            # Mark removal only. Process admission is NOT declared resumed
            # until a later live observation acknowledges absence/running.
            self.removed_monotonic=self.clock.monotonic()
            self.receipt['removed_at']=self.clock.time()
            self._save('marker_removed_resume_unverified')

    def verify_resume(self,snapshot):
        if self.receipt['state']!='marker_removed_resume_unverified':raise Refused('resume_not_pending')
        started,deadline=self._cleanup_times()
        with self.authority.guard(self.binding,self.owner,'resume_readback',deadline) as capability:
            _require_capability(capability)
            self._check_cleanup_time(started,deadline)
            if self.store.inspect() is not None:raise Refused('marker_replaced')
            self._wall_progress(self.receipt['removed_at'],self.removed_monotonic,self.clock.monotonic())
            validate_status(snapshot,self.binding,self.clock.time(),self.receipt['removed_at'],None,resuming=True)
            _require_capability(capability)
            self._save('resumed')

    @classmethod
    def recover_release(cls,binding,authority,store,clock):
        r=store.read_receipt()
        states={'prepared','drain_requested','release_intent','marker_removed_resume_unverified'}
        if not isinstance(r,dict) or r.get('version')!=1 or r.get('binding')!=binding.record() or r.get('state') not in states:
            raise Refused('receipt_binding')
        if not isinstance(r.get('owner'),str) or not re.fullmatch('[0-9a-f]{32}',r['owner']):raise Refused('invalid_receipt')
        if not isinstance(r.get('marker_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',r['marker_sha256']):raise Refused('invalid_receipt')
        if r.get('marker_token') is not None and (not isinstance(r['marker_token'],str) or not r['marker_token']):raise Refused('invalid_receipt')
        session=object.__new__(cls)
        session.binding=binding;session.authority=authority;session.store=store;session.clock=clock
        session.owner=r['owner'];session.marker_sha256=r['marker_sha256'];session.receipt=r
        session.release()
        return session
