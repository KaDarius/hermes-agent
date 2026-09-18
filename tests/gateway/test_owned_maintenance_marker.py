"""Real filesystem coverage for the maintenance controller's marker store."""
import hashlib
import json
import os
import stat

import pytest

from gateway import drain_marker_protocol as protocol


@pytest.mark.macos_only
def test_publish_and_remove_exact_generation(tmp_path):
    raw = json.dumps({'action': 'drain', 'maintenance_owner': 'a' * 32}).encode()
    published = []
    protocol.create_owned_marker(tmp_path, raw, published.append, require_valid=lambda: None)
    assert len(published) == 1
    assert (tmp_path / protocol.MARKER).read_bytes() == raw
    assert protocol.remove_owned_marker(tmp_path, published[0], hashlib.sha256(raw).hexdigest(), require_valid=lambda: None) == 'removed'
    assert not (tmp_path / protocol.MARKER).exists()


@pytest.mark.macos_only
def test_existing_marker_is_preserved(tmp_path):
    path = tmp_path / protocol.MARKER
    path.write_bytes(b'peer marker')
    with pytest.raises(protocol.DrainControlConflict):
        protocol.create_owned_marker(tmp_path, b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', lambda _: None, require_valid=lambda: None)
    assert path.read_bytes() == b'peer marker'


@pytest.mark.macos_only
def test_same_bytes_replacement_is_preserved(tmp_path):
    raw = b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    published = []
    protocol.create_owned_marker(tmp_path, raw, published.append, require_valid=lambda: None)
    path = tmp_path / protocol.MARKER
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(raw)
    replacement.replace(path)
    with pytest.raises(protocol.DrainControlConflict):
        protocol.remove_owned_marker(tmp_path, published[0], hashlib.sha256(raw).hexdigest(), require_valid=lambda: None)
    assert path.read_bytes() == raw


@pytest.mark.macos_only
def test_callback_failure_preserves_published_marker(tmp_path):
    raw = b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    def failed(_):
        raise RuntimeError('receipt unavailable')
    with pytest.raises(RuntimeError, match='receipt unavailable'):
        protocol.create_owned_marker(tmp_path, raw, failed, require_valid=lambda: None)
    assert (tmp_path / protocol.MARKER).read_bytes() == raw


@pytest.mark.macos_only
def test_cooperating_writer_lock_is_required(tmp_path):
    with protocol.marker_guard(tmp_path):
        with pytest.raises(protocol.DrainControlConflict, match='busy'):
            protocol.create_owned_marker(tmp_path, b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', lambda _: None, require_valid=lambda: None)


@pytest.mark.macos_only
@pytest.mark.parametrize('raw', [b'{}', b'{"maintenance_owner":true}', b'{"maintenance_owner":"peer"}', b'x' * 8193])
def test_invalid_payload_does_not_publish(tmp_path, raw):
    with pytest.raises(protocol.DrainControlConflict):
        protocol.create_owned_marker(tmp_path, raw, lambda _: None, require_valid=lambda: None)
    assert not (tmp_path / protocol.MARKER).exists()


@pytest.mark.macos_only
def test_symlink_preserves_target(tmp_path):
    target = tmp_path / 'peer'
    target.write_bytes(b'peer state')
    (tmp_path / protocol.MARKER).symlink_to(target)
    with pytest.raises(protocol.DrainControlConflict):
        protocol.create_owned_marker(tmp_path, b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', lambda _: None, require_valid=lambda: None)
    assert target.read_bytes() == b'peer state'
    assert (tmp_path / protocol.MARKER).is_symlink()


@pytest.mark.macos_only
def test_wrong_digest_refuses_and_absent_cleanup_is_idempotent(tmp_path):
    raw = b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    published = []
    protocol.create_owned_marker(tmp_path, raw, published.append, require_valid=lambda: None)
    with pytest.raises(protocol.DrainControlConflict):
        protocol.remove_owned_marker(tmp_path, published[0], '0' * 64, require_valid=lambda: None)
    assert (tmp_path / protocol.MARKER).read_bytes() == raw
    digest = hashlib.sha256(raw).hexdigest()
    assert protocol.remove_owned_marker(tmp_path, published[0], digest, require_valid=lambda: None) == 'removed'
    assert protocol.remove_owned_marker(tmp_path, published[0], digest, require_valid=lambda: None) == 'absent'


@pytest.mark.macos_only
def test_absent_retry_syncs_after_failed_unlink_durability(tmp_path, monkeypatch):
    raw = b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    published = []
    protocol.create_owned_marker(tmp_path, raw, published.append, require_valid=lambda: None)
    original = os.fsync
    attempts = []
    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            attempts.append(fd)
            if len(attempts) == 1:
                raise OSError('injected durability failure')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', sync)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(protocol.DrainControlConflict):
        protocol.remove_owned_marker(tmp_path, published[0], digest, require_valid=lambda: None)
    assert not (tmp_path / protocol.MARKER).exists()
    assert protocol.remove_owned_marker(tmp_path, published[0], digest, require_valid=lambda: None) == 'absent'
    assert len(attempts) == 2


@pytest.mark.macos_only
def test_denied_capability_does_not_touch_profile(tmp_path):
    before = set(tmp_path.iterdir())
    with pytest.raises(protocol.DrainControlConflict, match='capability'):
        protocol.create_owned_marker(tmp_path, b'{}', lambda _: None, require_valid=lambda: True)
    assert set(tmp_path.iterdir()) == before


@pytest.mark.macos_only
def test_revoked_capability_before_link_preserves_absence(tmp_path):
    calls = []
    def require_valid():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError('revoked')
    with pytest.raises(RuntimeError, match='revoked'):
        protocol.create_owned_marker(tmp_path, b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', lambda _: None, require_valid=require_valid)
    assert not (tmp_path / protocol.MARKER).exists()
    assert list(tmp_path.glob('.drain-stage-*')) == []


@pytest.mark.macos_only
def test_revoked_capability_before_unlink_preserves_marker(tmp_path):
    raw = b'{"maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    published = []
    protocol.create_owned_marker(tmp_path, raw, published.append, require_valid=lambda: None)
    calls = []
    def require_valid():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError('revoked')
    with pytest.raises(RuntimeError, match='revoked'):
        protocol.remove_owned_marker(tmp_path, published[0], hashlib.sha256(raw).hexdigest(), require_valid=require_valid)
    assert (tmp_path / protocol.MARKER).read_bytes() == raw
