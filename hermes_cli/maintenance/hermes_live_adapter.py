"""Native operations integration. No default live authority is granted here.

The installation owner must supply process-bound capability revalidators that
hold real writer exclusions. These adapters preserve refusal until that exists.
"""
import math
import json
import os
from pathlib import Path
import socket
import stat
import time

from .hermes_drain_marker_store import MarkerStore
from .hermes_job_install import Refused, _json


class DirectControlReader:
    """Strict read-only v1 transport, not loaded-code or peer-PID attestation.

    A direct socket in the approved canonical profile is required. Pointer
    discovery is intentionally unsupported. Replacement invalidates this
    reader; a fresh owner assessment is required before making another one.
    """
    LIMIT = 524288

    def __init__(self, profile, *, timeout=2.):
        self.profile = Path(profile)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise Refused('invalid_control_timeout')
        self.timeout = timeout
        self.endpoint = None

    def _endpoint(self):
        root = self.profile
        if not root.is_absolute() or root.resolve() != root:
            raise Refused('unsafe_control_profile')
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:  # windows-footgun: ok — package import rejects non-POSIX hosts
            raise Refused('unsafe_control_profile')
        path = root / 'gateway.sock'
        target = path.lstat()
        if not stat.S_ISSOCK(target.st_mode) or target.st_uid != os.getuid() or target.st_mode & 0o022:  # windows-footgun: ok — package import rejects non-POSIX hosts
            raise Refused('unsafe_control_socket')
        identity = (info.st_dev, info.st_ino, target.st_dev, target.st_ino, target.st_ctime_ns)
        if self.endpoint is not None and identity != self.endpoint:
            raise Refused('control_socket_replaced')
        self.endpoint = identity
        return path

    def query(self, verb):
        if verb not in ('identify', 'status'):
            raise Refused('unsupported_control_verb')
        deadline = time.monotonic() + self.timeout
        try:
            path = self._endpoint()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                def budget():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Refused('control_timeout')
                    client.settimeout(remaining)
                budget(); client.connect(str(path))
                budget(); client.sendall((json.dumps({'verb':verb,'id':1,'protocol':1})+'\n').encode())
                data = bytearray()
                while True:
                    budget(); part = client.recv(min(65536, self.LIMIT + 1 - len(data)))
                    if not part:
                        raise Refused('incomplete_control_response')
                    data.extend(part)
                    if len(data) > self.LIMIT:
                        raise Refused('oversized_control_response')
                    if b'\n' in data:
                        break
            self._endpoint()
            response = _json(bytes(data))
            _same(response, {'protocol':1,'id':1,'ok':True})
            if not isinstance(response.get('result'), dict):
                raise Refused('invalid_control_result')
            return response['result']
        except OSError:
            raise Refused('control_unavailable') from None

    def observe(self, binding, *, not_before, expected_marker, resuming=False):
        if str(self.profile) != binding.profile:
            raise Refused('observation_profile_mismatch')
        before = self.query('identify')
        status = self.query('status')
        after = self.query('identify')
        return adapt_native_observation(before, status, after, binding, now=time.time(),
                                         not_before=not_before, expected_marker=expected_marker,
                                         resuming=resuming)


def _same(record, expected):
    if not isinstance(record, dict) or any(k not in record or type(record[k]) is not type(v) or record[k] != v
                                           for k, v in expected.items()):
        raise Refused('observation_identity_mismatch')


def _fresh(value, now, not_before):
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (value, now, not_before)):
        raise Refused('invalid_observation_time')
    if not_before > now or value < not_before or value > now + 1 or now - value > 5:
        raise Refused('stale_or_future_observation')


def adapt_native_observation(before, status, after, binding, *, now, not_before,
                             expected_marker, resuming=False):
    """Normalize diagnostic evidence, NEVER manufacture admission authority.

    Transport trust and loaded-source attestation are separate prerequisites.
    The result deliberately lacks admission_routes; a live authority adapter
    must establish actual context-bound exclusions before controller use.
    """
    identity = {'pid': binding.pid, 'start_time': binding.start_time,
                'hermes_home': binding.profile, 'code_sha': binding.code_sha}
    _same(before, identity)
    _same(after, identity)
    _same(status, {'pid': binding.pid, 'answering_pid': binding.pid})
    _fresh(status.get('answered_at'), now, not_before)
    sample = status.get('maintenance_counters')
    _same(sample, {'writer_pid': binding.pid, 'writer_start_time': binding.start_time,
                   'profile': binding.profile, 'code_sha': binding.code_sha})
    _fresh(sample.get('observed_at'), now, not_before)
    drain = sample.get('drain')
    state = 'running' if resuming else 'draining'
    _same(status, {'gateway_state': state})
    _same(drain, {'valid': True, 'present': not resuming, 'requested': not resuming,
                  'gateway_state': state})
    _fresh(drain.get('observed_at'), now, not_before)
    if resuming:
        if expected_marker is not None:
            raise Refused('invalid_resume_marker')
        _same(drain, {'token': None, 'sha256': None})
    else:
        if (not isinstance(expected_marker, dict) or set(expected_marker) != {'token','sha256'}
                or any(not isinstance(v, str) or not v for v in expected_marker.values())):
            raise Refused('invalid_expected_marker')
        _same(drain, expected_marker)
    counters = sample.get('counters')
    if not isinstance(counters, dict) or set(counters) != {'messaging','cron','api'}:
        raise Refused('invalid_observation_counters')
    for item in counters.values():
        if (not isinstance(item, dict) or set(item) != {'valid','count'} or item['valid'] is not True
                or type(item['count']) is not int or item['count'] < 0):
            raise Refused('unknown_observation_counter')
    return {**binding.record(), 'observed_at': min(sample['observed_at'], drain['observed_at']),
            'gateway_state': state, 'marker_sha256': drain['sha256'],
            'counters': {k: dict(v) for k, v in counters.items()}}


class NativeMarkerStore(MarkerStore):
    """Use merged native marker operations; retain the existing receipt store."""

    def _native(self):
        # Before imports or any filesystem operation, preserve both defaults.
        self._capabilities()
        try:
            if not self.profile.is_absolute() or self.profile.resolve() != self.profile:
                raise Refused('unsafe_directory')
            root = self.profile.lstat()
        except OSError:
            raise Refused('profile_identity_unavailable') from None
        identity = root.st_dev, root.st_ino
        def require_valid():
            self._capabilities()
            try:
                current = self.profile.lstat()
                if (self.profile.resolve() != self.profile or not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev,current.st_ino) != identity
                        or current.st_uid != os.getuid() or current.st_mode & 0o022):  # windows-footgun: ok — package import rejects non-POSIX hosts
                    raise Refused('profile_identity_changed')
            except OSError:
                raise Refused('profile_identity_unavailable') from None
        require_valid()
        from gateway import drain_marker_protocol
        return drain_marker_protocol, require_valid

    def create_marker(self, data, published):
        native, require_valid = self._native()
        try:
            return native.create_owned_marker(self.profile, data, published,
                                              require_valid=require_valid)
        except native.DrainControlConflict as error:
            raise Refused(str(error)) from None

    def remove_owned(self, token, sha256):
        native, require_valid = self._native()
        try:
            return native.remove_owned_marker(self.profile, token, sha256,
                                              require_valid=require_valid)
        except native.DrainControlConflict as error:
            raise Refused(str(error)) from None

    def replace_marker(self, data):
        raise Refused('unconditional_marker_replacement_unavailable')
