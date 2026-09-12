"""Native marker-to-runner-to-status path in a temporary profile."""
import hashlib
import os

import pytest
from gateway import drain_control as dc, status
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner

pytestmark = pytest.mark.macos_only


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    obj, _ = make_restart_runner()
    # This fixture normally mocks status; integration must use the real writer.
    obj._update_runtime_status = GatewayRunner._update_runtime_status.__get__(obj)
    return obj


def test_observation_is_persisted_after_real_enter_and_exit(runner):
    dc.write_drain_request()
    expected = hashlib.sha256(dc.drain_request_path().read_bytes()).hexdigest()
    runner._observe_external_drain()
    record = status.read_runtime_status()['maintenance_counters']['drain']
    assert runner._external_drain_active is True
    assert record['valid'] is True and record['sha256'] == expected
    assert record['gateway_state'] == 'draining'
    dc.clear_drain_request()
    runner._observe_external_drain()
    record = status.read_runtime_status()['maintenance_counters']['drain']
    assert runner._external_drain_active is False
    assert record['valid'] is True and record['sha256'] is None
    assert record['gateway_state'] == 'running'


def test_busy_marker_invalidates_observation_without_releasing_drain(runner):
    import fcntl
    dc.write_drain_request(); runner._observe_external_drain()
    with (dc.drain_request_path().parent/'.drain-control.lock').open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(dc.DrainControlConflict): runner._observe_external_drain()
    assert runner._external_drain_active is True
    record = status.read_runtime_status()['maintenance_counters']['drain']
    assert record['valid'] is False
    assert 'sha256' not in record


def test_shutdown_cannot_produce_running_acceptance(runner):
    dc.write_drain_request(); runner._observe_external_drain()
    dc.clear_drain_request(); runner._draining = True
    runner._observe_external_drain()
    assert status.read_runtime_status()['maintenance_counters']['drain']['valid'] is False


def test_counter_updates_do_not_refresh_old_marker_observation(runner):
    dc.write_drain_request(); runner._observe_external_drain()
    before = status.read_runtime_status()['maintenance_counters']['drain']
    runner._persist_active_agents()
    assert status.read_runtime_status()['maintenance_counters']['drain'] == before
