"""Failed restart attempts preserve work and recover only their intake pause."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.restart import parse_restart_after_turn_timeout
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def runner(monkeypatch, tmp_path):
    r, _ = make_restart_runner()
    monkeypatch.setattr(gateway_run, '_hermes_home', tmp_path)
    r.stop = AsyncMock()
    r._launch_detached_restart_command = AsyncMock(return_value=True)
    r._restart_after_turn_timeout = 0
    r._running_agents['private-session'] = object()
    return r


@pytest.mark.parametrize('value', ['bad', -1, float('nan'), float('inf'), True])
def test_invalid_budget_is_not_a_default(value):
    with pytest.raises(ValueError):
        parse_restart_after_turn_timeout(value)


def test_unset_budget_and_explicit_values():
    assert parse_restart_after_turn_timeout(None) == 300
    assert parse_restart_after_turn_timeout(1800) == 1800
    assert parse_restart_after_turn_timeout(0) == 0


@pytest.mark.asyncio
async def test_failed_drain_recovers_intake_without_stop_or_helper(runner):
    assert runner.request_restart(detached=True)
    await runner._restart_task
    runner.stop.assert_not_awaited()
    runner._launch_detached_restart_command.assert_not_awaited()
    assert runner._draining is False
    assert runner._restart_requested is False
    assert runner._restart_task_started is False
    assert 'private-session' in runner._running_agents
    receipt = runner._restart_attempt_receipt
    assert receipt['outcome'] == 'refused'
    assert receipt['recovery'] == 'intake_restored'
    assert receipt['reason'] == 'timeout'
    assert 'private-session' not in json.dumps(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize('hold', ['external', 'maintenance', 'stop', 'stale'])
async def test_concurrent_owner_prevents_reopening(runner, hold):
    async def wait():
        if hold == 'external': runner._external_drain_active = True
        if hold == 'maintenance': runner._maintenance_pause_owner = 'other-owner'
        if hold == 'stop': runner._stop_task = asyncio.create_task(asyncio.sleep(0))
        if hold == 'stale': runner._restart_attempt_id = 'newer-owner'
        return False
    runner._await_active_work_before_restart = wait
    assert runner.request_restart()
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._draining is True


@pytest.mark.asyncio
async def test_probe_exception_refuses_without_leaking_message(runner):
    async def wait(): raise RuntimeError('secret-token-message-body')
    runner._await_active_work_before_restart = wait
    assert runner.request_restart()
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._restart_attempt_receipt['reason'] == 'drain_error'
    assert 'secret-token' not in json.dumps(runner._restart_attempt_receipt)


@pytest.mark.asyncio
async def test_preexisting_pause_is_never_taken_over(runner):
    runner._draining = True
    assert runner.request_restart() is False
    assert runner._draining is True
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnostic_failure_cannot_allow_stop(runner, monkeypatch):
    def fail(*args, **kwargs): raise OSError('secret-file-path')
    monkeypatch.setattr(gateway_run, 'atomic_json_write', fail)
    assert runner.request_restart()
    await runner._restart_task
    runner.stop.assert_not_awaited()
    runner._launch_detached_restart_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_marker_acquired_during_wait_survives(runner):
    from gateway.drain_control import write_drain_request, drain_request_path
    async def wait():
        write_drain_request(home=gateway_run._hermes_home)
        return False
    runner._await_active_work_before_restart = wait
    runner.request_restart()
    await runner._restart_task
    assert runner._draining is True
    assert drain_request_path(gateway_run._hermes_home).exists()
    assert runner._restart_attempt_receipt['recovery_refusal'] == 'external_marker'


@pytest.mark.asyncio
async def test_unknown_counter_is_not_idle(runner, monkeypatch):
    import cron.scheduler
    runner._running_agents.clear()
    monkeypatch.setattr(cron.scheduler, 'get_running_admission_count', lambda *, blocking=True: None)
    runner.request_restart()
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._restart_attempt_receipt['reason'] == 'drain_error'


@pytest.mark.asyncio
async def test_notification_for_refused_attempt_never_reports_success(runner, tmp_path):
    from gateway.platforms.base import MessageEvent, MessageType
    from tests.gateway.restart_test_helpers import make_restart_source
    event = MessageEvent(text='/restart', message_type=MessageType.TEXT,
                         source=make_restart_source(), message_id='m1', platform_update_id=42)
    await runner._handle_restart_command(event)
    await runner._restart_task
    marker = json.loads((tmp_path / '.restart_notify.json').read_text())
    assert marker['restart_attempt_id'] == runner._restart_attempt_id
    dedup = (tmp_path / '.restart_last_processed.json').read_bytes()
    assert await runner._send_restart_notification() is None
    assert (tmp_path / '.restart_last_processed.json').read_bytes() == dedup
    assert runner._is_stale_restart_redelivery(event)


@pytest.mark.asyncio
async def test_drain_success_requires_durable_diagnostic(runner, monkeypatch):
    runner._running_agents.clear()
    def fail(*args, **kwargs): raise OSError('private')
    monkeypatch.setattr(gateway_run, 'atomic_json_write', fail)
    runner.request_restart(detached=True)
    await runner._restart_task
    runner.stop.assert_not_awaited()
    runner._launch_detached_restart_command.assert_not_awaited()
    assert runner._restart_attempt_receipt['reason'] == 'diagnostic_failure'


@pytest.mark.asyncio
async def test_helper_failure_never_stops_or_reopens(runner):
    runner._running_agents.clear()
    runner._launch_detached_restart_command.side_effect = OSError('private')
    runner.request_restart(detached=True)
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._draining is True
    assert runner._restart_attempt_receipt['recovery_refusal'] == 'helper_launch_attempted'


def test_runtime_config_budget_source(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, '_hermes_home', tmp_path)
    monkeypatch.delenv('HERMES_RESTART_AFTER_TURN_TIMEOUT', raising=False)
    assert gateway_run.GatewayRunner._load_restart_after_turn_setting() == (300, 'default')
    (tmp_path / 'config.yaml').write_text('agent:\n  restart_after_turn_timeout: 1800\n')
    assert gateway_run.GatewayRunner._load_restart_after_turn_setting() == (1800, 'config')
    monkeypatch.setenv('HERMES_RESTART_AFTER_TURN_TIMEOUT', 'nan')
    with pytest.raises(ValueError): gateway_run.GatewayRunner._load_restart_after_turn_setting()


@pytest.mark.asyncio
async def test_fake_clock_timeout_records_budget_and_remaining_work(runner, monkeypatch):
    from types import SimpleNamespace
    now = [0.0]
    monkeypatch.setattr(gateway_run.asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: now[0]))
    async def tick(seconds): now[0] += seconds
    monkeypatch.setattr(gateway_run.asyncio, 'sleep', tick)
    runner._restart_after_turn_timeout = 0.2
    assert await runner._await_active_work_before_restart() is False
    assert runner._restart_drain_reason == 'timeout'
    assert runner._restart_remaining_work == 1
    assert now[0] >= 0.2


@pytest.mark.asyncio
async def test_unreadable_marker_never_reopens(runner, monkeypatch):
    import gateway.drain_control
    def fail(**kwargs): raise OSError('secret-hold')
    monkeypatch.setattr(gateway.drain_control, 'drain_request_snapshot', fail)
    runner.request_restart()
    await runner._restart_task
    assert runner._draining is True
    assert runner._restart_attempt_receipt['recovery_refusal'] == 'hold_state_unknown'
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_runtime_budget_changes_no_admission(runner):
    runner._restart_after_turn_timeout = float('inf')
    assert runner.request_restart() is False
    assert runner._draining is False
    assert runner._restart_requested is False


@pytest.mark.asyncio
async def test_stop_error_does_not_recover_intake(runner):
    runner._running_agents.clear()
    runner.stop.side_effect = RuntimeError('private-stop-info')
    runner.request_restart()
    await runner._restart_task
    assert runner._draining is True
    assert runner._restart_attempt_receipt['reason'] == 'transition_error'
    assert runner._restart_attempt_receipt['recovery_refusal'] == 'stop_attempted'


@pytest.mark.asyncio
async def test_stale_attempt_cannot_overwrite_newer_in_memory_receipt(runner):
    newer = {'attempt_id': 'newer', 'outcome': 'waiting'}
    async def wait():
        runner._restart_attempt_id = 'newer'
        runner._restart_attempt_receipt = newer
        return False
    runner._await_active_work_before_restart = wait
    runner.request_restart()
    await runner._restart_task
    assert runner._restart_attempt_receipt is newer


@pytest.mark.asyncio
async def test_api_configured_but_missing_is_unknown(runner):
    from gateway.config import Platform, PlatformConfig
    runner._running_agents.clear()
    runner.config.platforms[Platform.API_SERVER] = PlatformConfig(enabled=True)
    runner.request_restart()
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._restart_attempt_receipt['reason'] == 'drain_error'


@pytest.mark.asyncio
async def test_counter_lock_contention_is_unknown_without_blocking(runner, monkeypatch):
    import cron.scheduler
    calls = []
    def read(*, blocking=True):
        calls.append(blocking)
        raise RuntimeError('busy')
    monkeypatch.setattr(cron.scheduler, 'get_running_admission_count', read)
    runner.request_restart()
    await runner._restart_task
    assert calls == [False]
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_scheduler_not_imported_to_prove_idle(runner, monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, 'cron.scheduler')
    runner._running_agents.clear()
    runner.request_restart()
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert 'cron.scheduler' not in sys.modules


@pytest.mark.asyncio
async def test_helper_failure_and_failed_final_log_cannot_leave_success(runner, monkeypatch, tmp_path):
    runner._running_agents.clear()
    write = gateway_run.atomic_json_write
    def selectively_fail(path, data, **kwargs):
        if data.get('outcome') == 'refused': raise OSError('private')
        return write(path, data, **kwargs)
    monkeypatch.setattr(gateway_run, 'atomic_json_write', selectively_fail)
    runner._launch_detached_restart_command.return_value = False
    runner.request_restart(detached=True)
    await runner._restart_task
    data = json.loads((tmp_path / 'logs' / 'restart_attempts' / (runner._restart_attempt_id + '.json')).read_text())
    assert data['outcome'] == 'transition_pending'
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_signal_attempt_does_not_steal_pending_command_id(runner):
    runner._restart_command_pending = 'a' * 32
    runner.request_restart(via_service=True)
    assert runner._restart_attempt_id != runner._restart_command_pending
    await runner._restart_task


@pytest.mark.asyncio
async def test_notification_cleanup_preserves_replacement(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.platforms.base import SendResult
    r, adapter = make_restart_runner()
    monkeypatch.setattr(gateway_run, '_hermes_home', tmp_path)
    path = tmp_path / '.restart_notify.json'
    old = {'platform': 'telegram', 'chat_id': '42'}
    new = {'platform': 'telegram', 'chat_id': '99'}
    path.write_text(json.dumps(old))
    async def send(*args, **kwargs):
        path.write_text(json.dumps(new))
        return SendResult(success=True, message_id='1')
    adapter.send = send
    await r._send_restart_notification()
    assert json.loads(path.read_text()) == new


def test_invalid_budget_diagnostic_is_private(runner, tmp_path):
    runner._restart_after_turn_timeout = 'private-token-value'
    assert runner.request_restart() is False
    data = [json.loads(p.read_text()) for p in (tmp_path / 'logs' / 'restart_attempts').glob('*.json')]
    assert len(data) == 1
    assert data[0]['reason'] == 'invalid_timeout'
    assert 'private-token-value' not in json.dumps(data)


@pytest.mark.asyncio
async def test_proven_missing_binary_recovers_owned_intake(runner, monkeypatch):
    runner._running_agents.clear()
    runner._launch_detached_restart_command = gateway_run.GatewayRunner._launch_detached_restart_command.__get__(runner)
    monkeypatch.setattr(gateway_run, '_resolve_hermes_bin', lambda: [])
    assert runner.request_restart(detached=True)
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._draining is False
    assert runner._restart_task_started is False
    assert runner._restart_attempt_receipt['recovery'] == 'intake_restored'


@pytest.mark.asyncio
async def test_existing_helper_keeps_intake_held(runner, monkeypatch):
    runner._running_agents.clear()
    runner._detached_restart_helper_started = True
    runner._launch_detached_restart_command = gateway_run.GatewayRunner._launch_detached_restart_command.__get__(runner)
    monkeypatch.setattr(gateway_run, '_resolve_hermes_bin', lambda: [])
    assert runner.request_restart(detached=True)
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._draining is True


@pytest.mark.asyncio
async def test_no_launch_does_not_release_external_hold(runner):
    runner._running_agents.clear()
    async def no_launch():
        runner._external_drain_active = True
        return False
    runner._launch_detached_restart_command = no_launch
    runner.request_restart(detached=True)
    await runner._restart_task
    runner.stop.assert_not_awaited()
    assert runner._draining is True


@pytest.mark.asyncio
async def test_invalid_budget_chat_returns_refusal(runner):
    from gateway.platforms.base import MessageEvent, MessageType
    from tests.gateway.restart_test_helpers import make_restart_source
    runner._restart_after_turn_timeout = 'invalid'
    event = MessageEvent(text='/restart', message_type=MessageType.TEXT,
                         source=make_restart_source(), message_id='invalid-budget')
    reply = await runner._handle_restart_command(event)
    assert 'refused' in reply.lower()
    assert runner._restart_command_source is None
    assert runner._draining is False
    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('detached', [False, True])
async def test_real_stop_settles_receipt_before_shutdown_waiter_returns(runner, monkeypatch, tmp_path, detached):
    from unittest.mock import Mock
    runner._running_agents.clear()
    runner.stop = gateway_run.GatewayRunner.stop.__get__(runner)
    # Exercise real teardown and event ordering, isolate all external cleanup.
    monkeypatch.setattr('tools.process_registry.process_registry.kill_all', lambda: [])
    monkeypatch.setattr('tools.terminal_tool.cleanup_all_environments', lambda: None)
    monkeypatch.setattr('tools.browser_tool.cleanup_all_browsers', lambda: None)
    monkeypatch.setattr('agent.auxiliary_client.shutdown_cached_clients', lambda: None)
    monkeypatch.setattr('gateway.status.remove_pid_file', lambda: None)
    monkeypatch.setattr('gateway.status.release_gateway_runtime_lock', lambda: None)
    runner._shutdown_executor = Mock()
    calls = []
    async def launch_once():
        calls.append(True)
        if runner._detached_restart_helper_started:
            raise RuntimeError('helper already exists')
        runner._detached_restart_helper_started = True
        return True
    runner._launch_detached_restart_command = launch_once
    async def process_exit_boundary():
        await runner.wait_for_shutdown()
        assert runner._restart_attempt_receipt['outcome'] == 'restart_ready'
    waiter = asyncio.create_task(process_exit_boundary())
    await asyncio.sleep(0)
    assert runner.request_restart(via_service=not detached, detached=detached)
    (tmp_path / '.restart_notify.json').write_text(json.dumps({
        'platform': 'telegram', 'chat_id': '42',
        'restart_attempt_id': runner._restart_attempt_id,
    }))
    results = await asyncio.gather(waiter, runner._restart_task, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    fresh, adapter = make_restart_runner()
    await fresh._send_restart_notification()
    assert adapter.sent
    assert len(calls) == int(detached)


@pytest.mark.asyncio
async def test_unrelated_shutdown_does_not_wait_for_pending_restart(runner):
    pending = asyncio.create_task(asyncio.Event().wait())
    runner._restart_task = pending
    runner._shutdown_event.set()
    try:
        await asyncio.wait_for(runner.wait_for_shutdown(), timeout=2)
        assert not pending.done()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_wait_refuses_when_shutdown_begins(runner):
    runner._running = False
    runner._restart_after_turn_timeout = 1800
    assert await asyncio.wait_for(runner._await_active_work_before_restart(), timeout=2) is False
    assert runner._restart_drain_reason == 'shutdown_in_progress'


@pytest.mark.windows_only
@pytest.mark.asyncio
async def test_real_windows_helper_double_spawn_failure_returns_false(monkeypatch):
    import subprocess
    r, _ = make_restart_runner()
    monkeypatch.setattr(gateway_run, '_resolve_hermes_bin', lambda: ['hermes'])
    def fail(*args, **kwargs): raise OSError('private spawn details')
    monkeypatch.setattr(subprocess, 'Popen', fail)
    assert await r._launch_detached_restart_command() is False
    assert r._detached_restart_helper_started is False


@pytest.mark.macos_only
@pytest.mark.asyncio
async def test_detached_shell_deadline_refuses_live_process(monkeypatch, tmp_path):
    await _assert_detached_shell_deadline_refuses_live_process(monkeypatch, tmp_path)


@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_linux_detached_shell_deadline_refuses_live_process(monkeypatch, tmp_path):
    await _assert_detached_shell_deadline_refuses_live_process(monkeypatch, tmp_path)


async def _assert_detached_shell_deadline_refuses_live_process(monkeypatch, tmp_path):
    """Execute generated helper under inert shell functions, never a real PID."""
    import subprocess
    r, _ = make_restart_runner()
    monkeypatch.setattr(gateway_run, '_resolve_hermes_bin', lambda: ['hermes'])
    captured = []
    real_run = subprocess.run
    with monkeypatch.context() as patcher:
        patcher.setattr(subprocess, 'Popen', lambda args, **kw: captured.append(args))
        assert await r._launch_detached_restart_command() is True
    args = captured[0]
    script = args[args.index('-lc') + 1]
    counter = tmp_path / 'clock'
    counter.write_text('0')
    env = {'PATH': '/usr/bin:/bin', 'TEST_CLOCK': str(counter)}
    preamble = '''
kill() { return 0; }
date() { n=$(cat "$TEST_CLOCK"); n=$((n + 100)); echo "$n" > "$TEST_CLOCK"; echo "$n"; }
hermes() { echo UNSAFE_RESTART; }
'''
    result = real_run(['/bin/bash', '-c', preamble + script], env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 1
    assert 'UNSAFE_RESTART' not in result.stdout
