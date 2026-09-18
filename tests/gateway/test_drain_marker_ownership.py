"""Real native marker operations on disposable profiles; no live gateway."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from gateway import drain_control as dc

pytestmark = pytest.mark.macos_only


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    return tmp_path


def test_native_write_creates_new_home(profile):
    home = profile/'new'/'profile'
    dc.write_drain_request(home=home)
    assert dc.drain_requested(home=home) is True
    assert dc.clear_drain_request(home=home) is True


def test_native_clear_absent_home_is_idempotent(profile):
    home = profile/'not-created'
    assert dc.clear_drain_request(home=home) is False
    assert not home.exists()


@pytest.mark.parametrize('action', ['write', 'clear'])
def test_native_caller_preserves_maintenance_owned_marker(profile, action):
    path = dc.drain_request_path()
    raw = json.dumps({'action': 'drain', 'maintenance_owner': 'transaction-a'}).encode()
    path.write_bytes(raw)
    with pytest.raises(RuntimeError):
        dc.write_drain_request() if action == 'write' else dc.clear_drain_request()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('action', ['write', 'clear'])
def test_unknown_ownership_is_preserved(profile, action):
    path = dc.drain_request_path(); path.write_bytes(b'{broken')
    with pytest.raises(RuntimeError):
        dc.write_drain_request() if action == 'write' else dc.clear_drain_request()
    assert path.read_bytes() == b'{broken'


@pytest.mark.parametrize('action', ['write', 'clear'])
@pytest.mark.macos_only
def test_real_child_writer_obeys_shared_protocol_lock(profile, action):
    import fcntl
    dc.write_drain_request()
    before = dc.drain_request_path().read_bytes()
    code = "from gateway import drain_control as d; import sys; exec(\"try:\\n d.write_drain_request() if sys.argv[1]=='write' else d.clear_drain_request()\\n print('unexpected')\\nexcept RuntimeError: print('refused')\")"
    with (profile/'.drain-control.lock').open('a+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, '-B', '-c', code, action], capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'refused'
    assert dc.drain_request_path().read_bytes() == before
    assert dc.clear_drain_request() is True  # positive control after release


def test_snapshot_binds_policy_and_fingerprint_to_one_read(profile, monkeypatch):
    dc.write_drain_request(principal='first')
    original = dc.drain_request_path().read_bytes()
    seen = []
    def policy(body):
        seen.append(body['principal'])
        # An intentionally noncooperating writer must not change the bytes
        # whose policy was evaluated inside this already-captured sample.
        dc.drain_request_path().write_text('{"principal":"later"}')
        return False
    monkeypatch.setattr(dc, '_marker_is_stale', policy)
    snapshot = dc.drain_request_snapshot()
    assert seen == ['first']
    assert snapshot['sha256'] == hashlib.sha256(original).hexdigest()
    assert snapshot['requested'] is True


def test_snapshot_handles_absence_expiry_and_generation(profile, monkeypatch):
    assert dc.drain_request_snapshot() == {'present': False, 'requested': False, 'sha256': None, 'token': None}
    dc.write_drain_request()
    first = dc.drain_request_snapshot()
    path = dc.drain_request_path(); temporary = profile/'replacement'
    temporary.write_bytes(path.read_bytes()); os.replace(temporary, path)
    second = dc.drain_request_snapshot()
    assert first['sha256'] == second['sha256']
    assert first['token'] != second['token']
    monkeypatch.setattr(dc, '_marker_is_stale', lambda body: True)
    expired = dc.drain_request_snapshot()
    assert expired['present'] is True and expired['requested'] is False
    assert expired['sha256'] == second['sha256']


@pytest.mark.parametrize('name', ['.drain_request.json', '.drain-control.lock'])
@pytest.mark.macos_only
def test_unsafe_symlink_never_touches_target(profile, name):
    target = profile/'untouched'; target.write_text('peer')
    (profile/name).symlink_to(target)
    with pytest.raises(RuntimeError): dc.write_drain_request()
    assert target.read_text() == 'peer'


@pytest.mark.macos_only
def test_fifo_snapshot_refuses_without_blocking(profile):
    os.mkfifo(dc.drain_request_path(), 0o600)
    code = "from gateway import drain_control as d; exec(\"try:\\n d.drain_request_snapshot()\\n print('unexpected')\\nexcept RuntimeError: print('refused')\")"
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'refused'


def test_legacy_reader_cannot_block_on_fifo(profile):
    os.mkfifo(dc.drain_request_path(), 0o600)
    code = "from gateway import drain_control as d; assert d.read_drain_request()=={}; print('unknown')"
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'unknown'


@pytest.mark.parametrize('action', ['drain', 'cancel'])
def test_dashboard_conflict_is_explicit_and_preserves_owner(profile, action):
    from fastapi import HTTPException
    from hermes_cli.web_server import gateway_drain
    raw = b'{"action":"drain","maintenance_owner":"transaction-a"}'
    dc.drain_request_path().write_bytes(raw)
    async def body(): return {'action': action}
    request = SimpleNamespace(json=body, state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc: asyncio.run(gateway_drain(request))
    assert exc.value.status_code == 409
    assert dc.drain_request_path().read_bytes() == raw
