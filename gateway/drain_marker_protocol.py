"""POSIX cooperating-writer protocol for drain markers.

The installer and native callers share .drain-control.lock. This serializes
participating writers; it is not authority over dispatchers or old writers.
Directory/lock replacement outside this protocol is not supported. Windows
keeps its existing native drain path until equivalent protocol support exists.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

MARKER = '.drain_request.json'
LOCK = '.drain-control.lock'
MAX_MARKER = 8192


class DrainControlConflict(RuntimeError):
    """Marker ownership or safe access cannot be established; preserve it."""


def _safe_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()  # windows-footgun: ok — POSIX marker_guard callers only
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise DrainControlConflict('unsafe_drain_file')


def _identity(info):
    return info.st_dev, info.st_ino


def _token(info):
    return f'{info.st_dev}:{info.st_ino}:{info.st_ctime_ns}'


@contextmanager
def marker_guard(home: Path):
    if os.name != 'posix':
        raise DrainControlConflict('drain_protocol_unavailable')
    import fcntl

    root = Path(home).resolve()
    directory = lockfd = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        ds = os.fstat(directory)
        if ds.st_uid != os.getuid() or ds.st_mode & 0o022:  # windows-footgun: ok — POSIX guard above
            raise DrainControlConflict('unsafe_drain_directory')
        lockfd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        ls = os.fstat(lockfd)
        _safe_file(ls)
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DrainControlConflict('drain_marker_busy') from None

        def check():
            current_root = os.stat(root, follow_symlinks=False)
            current_lock = os.stat(LOCK, dir_fd=directory, follow_symlinks=False)
            if _identity(current_root) != _identity(ds) or _identity(current_lock) != _identity(ls):
                raise DrainControlConflict('drain_protocol_path_changed')
            if current_root.st_uid != os.getuid() or current_root.st_mode & 0o022:  # windows-footgun: ok — POSIX guard above
                raise DrainControlConflict('unsafe_drain_directory')
            _safe_file(current_lock)

        check()
        yield directory, check
    except OSError:
        raise DrainControlConflict('drain_marker_unreadable') from None
    finally:
        if lockfd is not None:
            os.close(lockfd)
        if directory is not None:
            os.close(directory)


def read_locked(fd):
    try:
        opened = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(opened)
        _safe_file(info)
        with os.fdopen(opened, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_MARKER + 1)
        if len(raw) > MAX_MARKER or _token(os.fstat(opened)) != _token(info):
            raise DrainControlConflict('drain_marker_changed_or_oversized')
        return raw, _token(info)
    finally:
        os.close(opened)


def marker_body(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('invalid constant')

    try:
        body = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(body, dict):
            raise ValueError('not an object')
        return body
    except (ValueError, UnicodeError, RecursionError):
        raise DrainControlConflict('drain_marker_ownership_unknown') from None


def require_unreserved(found):
    if found is not None and 'maintenance_owner' in marker_body(found[0]):
        raise DrainControlConflict('drain_marker_owned_by_maintenance')


def replace_locked(fd, payload, check):
    raw = (json.dumps(payload, sort_keys=True, allow_nan=False) + '\n').encode()
    if len(raw) > MAX_MARKER:
        raise DrainControlConflict('drain_marker_oversized')
    temporary = '.drain-stage-' + uuid.uuid4().hex
    opened = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(opened, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        check()
        os.replace(temporary, MARKER, src_dir_fd=fd, dst_dir_fd=fd)
        temporary = None
        os.fsync(fd)
        check()
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=fd)


def describe(found):
    if found is None:
        return {'present': False, 'requested': False, 'sha256': None, 'token': None}
    return {'present': True, 'sha256': hashlib.sha256(found[0]).hexdigest(), 'token': found[1]}
