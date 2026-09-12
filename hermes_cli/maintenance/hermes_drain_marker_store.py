"""Filesystem adapter for the proposed cooperating drain-marker protocol.

NOT wired to Beta. Both authority and protocol-adoption callbacks default to
refusal, before filesystem access. All marker writers (including runtime and
UI begin/clear paths) MUST adopt the stable .drain-control.lock protocol before
require_protocol can succeed. This adapter cannot exclude old/direct writers.

Callbacks are context-bound opaque capability revalidators, not boolean flags;
only None means success. Their implementation must bind this profile/operation
and current process/source/lease. This module does not supply that authority.
Compare/read/unlink is indivisible only among writers using this same lock.
The trusted owner must not replace the lock or profile directory outside that
protocol. Explicit checks reject observed replacement; hostile TOCTOU races
outside the cooperating-writer contract are not claimed safe.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .hermes_job_install import Refused, _json

MARKER='.drain_request.json'
LOCK='.drain-control.lock'
PROTOCOL_VERSION=1
MAX_MARKER=8192
MAX_RECEIPT=65536


def _deny_authority():raise Refused('live_marker_authority_unavailable')
def _deny_protocol():raise Refused('marker_protocol_unverified')

def _require(callback):
    if not callable(callback) or callback() is not None:raise Refused('invalid_marker_capability')

def fsync_dir(fd):os.fsync(fd)

def _identity(s):return s.st_dev,s.st_ino

def _token(s):
    # ctime detects in-place rewrites and inode reuse, including identical bytes.
    # Capture only after the publication temporary hard-link is removed: its
    # removal changes ctime, so a token captured before then would be stale.
    return f'{s.st_dev}:{s.st_ino}:{s.st_ctime_ns}'

def _safe_file(s):
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1 or s.st_mode&0o022:  # windows-footgun: ok — package import rejects non-POSIX hosts
        raise Refused('unsafe_file')

def _read_at(fd,name,limit):
    # Open without waiting on a FIFO, then validate the opened descriptor.
    # O_NONBLOCK has no effect on ordinary regular-file reads.
    try:opened=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    except FileNotFoundError:return None
    except OSError:raise Refused('unsafe_file') from None
    try:
        s=os.fstat(opened);_safe_file(s)
        with os.fdopen(opened,'rb',closefd=False) as f:data=f.read(limit+1)
        if len(data)>limit:raise Refused('oversized_file')
        if _token(os.fstat(opened))!=_token(s):raise Refused('file_changed_during_read')
        return data,_token(s)
    finally:os.close(opened)


class MarkerStore:
    def __init__(self,profile,receipt_dir,*,require_authority=_deny_authority,require_protocol=_deny_protocol):
        # No I/O here; unresolved/default capabilities cannot create even a lock.
        self.profile=Path(profile);self.receipt_dir=Path(receipt_dir)
        self.require_authority=require_authority;self.require_protocol=require_protocol

    def _capabilities(self):
        _require(self.require_authority);_require(self.require_protocol)

    @contextlib.contextmanager
    def _guard(self,root,lock_name):
        self._capabilities()
        if not root.is_absolute() or root.resolve()!=root:raise Refused('unsafe_directory')
        directory=lockfd=None
        try:
            try:directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            except OSError:raise Refused('unsafe_directory') from None
            ds=os.fstat(directory)
            if not stat.S_ISDIR(ds.st_mode) or ds.st_uid!=os.getuid() or ds.st_mode&0o022:raise Refused('unsafe_directory')  # windows-footgun: ok — package import rejects non-POSIX hosts
            try:lockfd=os.open(lock_name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=directory)
            except OSError:raise Refused('unsafe_lock') from None
            ls=os.fstat(lockfd);_safe_file(ls)
            try:fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise Refused('marker_lock_busy') from None
            def check():
                self._capabilities()
                try:
                    current_root=os.stat(root,follow_symlinks=False)
                    current_lock=os.stat(lock_name,dir_fd=directory,follow_symlinks=False)
                except OSError:raise Refused('protocol_path_changed') from None
                if _identity(current_root)!=_identity(ds) or _identity(current_lock)!=_identity(ls):raise Refused('protocol_path_changed')
                if not stat.S_ISDIR(current_root.st_mode) or current_root.st_mode&0o022:raise Refused('unsafe_directory')
                _safe_file(current_lock)
            check()
            yield directory,check
        finally:
            if lockfd is not None:os.close(lockfd)
            if directory is not None:os.close(directory)

    def marker_guard(self):return self._guard(self.profile,LOCK)

    @staticmethod
    def _describe(found):
        if found is None:return None
        raw,token=found
        return {'token':token,'sha256':hashlib.sha256(raw).hexdigest()}

    def inspect(self):
        with self.marker_guard() as (fd,check):
            found=_read_at(fd,MARKER,MAX_MARKER);check()
            return self._describe(found)

    def _stage(self,fd,data):
        name='.drain-stage-'+uuid.uuid4().hex
        opened=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        try:
            with os.fdopen(opened,'wb') as f:
                f.write(data);f.flush();os.fsync(f.fileno())
        except BaseException:
            os.unlink(name,dir_fd=fd)
            raise
        return name

    @staticmethod
    def _payload(data):
        if not isinstance(data,bytes) or not 0<len(data)<=MAX_MARKER:raise Refused('invalid_marker_payload')

    def create_marker(self,data,published):
        self._payload(data)
        if not callable(published):raise Refused('invalid_publication_callback')
        with self.marker_guard() as (fd,check):
            if _read_at(fd,MARKER,MAX_MARKER) is not None:raise Refused('marker_exists')
            temporary=self._stage(fd,data)
            try:
                check()
                try:os.link(temporary,MARKER,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                except FileExistsError:raise Refused('marker_exists') from None
                # A failure between publication and the callback is ambiguous
                # to the controller. Preserve the marker; never infer absence.
                os.unlink(temporary,dir_fd=fd);temporary=None
                found=_read_at(fd,MARKER,MAX_MARKER)
                if found is None or found[0]!=data:raise Refused('publication_readback')
                published(found[1])
                fsync_dir(fd)
                check()
            finally:
                if temporary is not None:os.unlink(temporary,dir_fd=fd)

    def replace_marker(self,data):
        """Protocol operation for a separately authorized native begin/refresh.
        This is NOT used by the controller's exclusive initial publication.
        """
        self._payload(data)
        with self.marker_guard() as (fd,check):
            _read_at(fd,MARKER,MAX_MARKER)  # refuse symlink/unsafe prior target
            temporary=self._stage(fd,data)
            try:
                check();os.replace(temporary,MARKER,src_dir_fd=fd,dst_dir_fd=fd);temporary=None
                fsync_dir(fd);check()
                return self._describe(_read_at(fd,MARKER,MAX_MARKER))
            finally:
                if temporary is not None:os.unlink(temporary,dir_fd=fd)

    def remove_owned(self,token,sha256):
        with self.marker_guard() as (fd,check):
            found=self._describe(_read_at(fd,MARKER,MAX_MARKER))
            if found is None:
                # Retry durability when an earlier unlink succeeded but its
                # directory sync failed before completion was journaled.
                check()
                fsync_dir(fd)
                return 'absent'
            if found!={'token':token,'sha256':sha256}:raise Refused('marker_replaced')
            check()
            os.unlink(MARKER,dir_fd=fd)
            fsync_dir(fd)
            return 'removed'

    def read_receipt(self):
        with self._guard(self.receipt_dir,'.maintenance-receipt.lock') as (fd,check):
            found=_read_at(fd,'transaction.json',MAX_RECEIPT);check()
            return None if found is None else _json(found[0])

    def write_receipt(self,value):
        if not isinstance(value,dict) or not isinstance(value.get('owner'),str) or not value['owner'] or not isinstance(value.get('binding'),dict):raise Refused('invalid_receipt')
        try:raw=(json.dumps(value,sort_keys=True,allow_nan=False)+'\n').encode()
        except (TypeError,ValueError,UnicodeError):raise Refused('invalid_receipt') from None
        if len(raw)>MAX_RECEIPT:raise Refused('oversized_receipt')
        with self._guard(self.receipt_dir,'.maintenance-receipt.lock') as (fd,check):
            prior=_read_at(fd,'transaction.json',MAX_RECEIPT)
            if prior is not None:
                previous=_json(prior[0])
                if not isinstance(previous,dict) or any(previous.get(k)!=value[k] for k in ('owner','binding')):raise Refused('receipt_owner_mismatch')
            temporary=self._stage(fd,raw)
            try:
                check();os.replace(temporary,'transaction.json',src_dir_fd=fd,dst_dir_fd=fd);temporary=None
                fsync_dir(fd)
                if _read_at(fd,'transaction.json',MAX_RECEIPT)[0]!=raw:raise Refused('receipt_readback')
            finally:
                if temporary is not None:os.unlink(temporary,dir_fd=fd)
