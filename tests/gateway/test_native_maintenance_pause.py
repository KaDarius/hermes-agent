"""Owned native admission pause; no shutdown, credentials, or live services."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from cron import scheduler
from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.platforms.api_server import APIServerAdapter
from gateway.control_socket import GatewayControlServer

OWNER = 'a' * 32
OTHER = 'b' * 32

@pytest.fixture
def runner(monkeypatch):
    obj = object.__new__(GatewayRunner)
    obj._running = True
    obj._draining = False
    obj._external_drain_active = False
    obj._running_agents = {}
    obj.adapters = {}
    monkeypatch.setattr(scheduler, '_maintenance_pause_owner', None, raising=False)
    yield obj
    timer = getattr(obj, '_maintenance_pause_timer', None)
    if timer:
        timer.cancel()

@pytest.mark.asyncio
async def test_pause_blocks_new_registrations_and_resume_is_owned(runner):
    result = runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    assert result['state'] == 'paused'
    assert scheduler.try_register_running_job('new-during-pause') is False
    assert runner._claim_active_session_slot('new-session', None)[1] is not None
    assert runner.maintenance_resume({'owner': OTHER})['state'] == 'refused'
    assert scheduler.try_register_running_job('still-blocked') is False
    assert runner.maintenance_resume({'owner': OWNER})['state'] == 'running'
    assert scheduler.try_register_running_job('after-resume')
    scheduler.release_running_job('after-resume')

@pytest.mark.asyncio
async def test_existing_cron_can_finish_and_external_drain_survives(runner):
    assert scheduler.try_register_running_job('existing')
    try:
        runner._external_drain_active = True
        runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
        scheduler.release_running_job('existing')
        assert 'existing' not in scheduler.get_running_job_ids()
        assert runner.maintenance_resume({'owner': OWNER})['state'] == 'running'
        assert runner._external_drain_active is True
    finally:
        scheduler.release_running_job('existing')

@pytest.mark.asyncio
async def test_direct_fire_waits_without_losing_claimed_work(runner, monkeypatch):
    import threading
    attempted = threading.Event()
    executed = []
    real_wait = scheduler._maintenance_resumed.wait
    def waiting(timeout=None):
        attempted.set()
        return real_wait(timeout)
    monkeypatch.setattr(scheduler._maintenance_resumed, 'wait', waiting)
    monkeypatch.setattr(scheduler, '_run_one_job_body', lambda job, **kw: executed.append(job['id']) or True)
    runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    task = asyncio.create_task(asyncio.to_thread(scheduler.run_one_job, {'id': 'unadmitted'}))
    try:
        assert await asyncio.to_thread(attempted.wait, 5)
        assert not executed
        assert 'unadmitted' in scheduler.get_running_job_ids()
        runner.maintenance_resume({'owner': OWNER})
        assert await asyncio.wait_for(task, 5)
        assert executed == ['unadmitted']
        assert 'unadmitted' not in scheduler.get_running_job_ids()
    finally:
        runner.maintenance_resume({'owner': OWNER})
        await asyncio.wait_for(task, 5)

@pytest.mark.asyncio
async def test_api_admission_refuses_but_completion_is_untouched(runner):
    api = APIServerAdapter(PlatformConfig(enabled=True))
    api.gateway_runner = runner
    runner.adapters[Platform.API_SERVER] = api
    api._pending_agent_requests = 1
    runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    assert api._draining_response().status == 503
    assert api.strict_active_agent_work_count() == 1
    assert runner._running is True and runner._draining is False

@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {}, {'owner': 'short'}, {'owner': OWNER, 'seconds': True},
    {'owner': OWNER, 'seconds': 0}, {'owner': OWNER, 'seconds': 601},
])
async def test_bad_pause_does_not_change_admission(runner, payload):
    assert runner.maintenance_pause(payload)['state'] == 'refused'
    assert not runner._maintenance_is_paused()

@pytest.mark.asyncio
async def test_existing_owner_cannot_be_replaced(runner):
    runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    assert runner.maintenance_pause({'owner': OTHER, 'seconds': 60})['state'] == 'refused'
    assert runner.maintenance_resume({'owner': OTHER})['state'] == 'refused'
    assert runner._maintenance_is_paused()

@pytest.mark.asyncio
async def test_control_handlers_run_on_owning_loop_and_require_protocol(tmp_path):
    loop = asyncio.get_running_loop()
    calls = []
    def pause(request):
        assert asyncio.get_running_loop() is loop
        calls.append(request)
        return {'state': 'paused'}
    server = GatewayControlServer(tmp_path, loop_verb_handlers={'maintenance_pause': pause})
    valid = json.dumps({'verb': 'maintenance_pause', 'id': 1, 'protocol': 1, 'owner': OWNER}).encode()
    assert json.loads(server.handle_loop_request_line(valid))['ok'] is True
    assert len(calls) == 1
    invalid = valid.replace(b'"protocol": 1', b'"protocol": 2')
    assert json.loads(server.handle_loop_request_line(invalid))['ok'] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_registration_after_an_earlier_dispatch_check_is_blocked(runner):
    import threading
    dispatch_checked = threading.Event()
    register_now = threading.Event()
    result = []
    def late_tick():
        dispatch_checked.set()
        assert register_now.wait(5)
        result.append(scheduler.try_register_running_job('late-tick'))
    thread = threading.Thread(target=late_tick)
    thread.start()
    try:
        assert dispatch_checked.wait(5)
        runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
        register_now.set()
        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive()
        assert result == [False]
    finally:
        register_now.set()
        thread.join(5)
        scheduler.release_running_job('late-tick')

@pytest.mark.asyncio
async def test_waiting_internal_obligation_is_released_on_resume(runner):
    runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    waiting = asyncio.create_task(runner._wait_maintenance_admission())
    await asyncio.sleep(0)  # explicit task scheduling, not a timing bound
    assert not waiting.done()
    runner.maintenance_resume({'owner': OWNER})
    await asyncio.wait_for(waiting, 5)

@pytest.mark.asyncio
async def test_pause_expiry_releases_only_its_owner(runner):
    runner.maintenance_pause({'owner': OWNER, 'seconds': 1})
    event = runner._maintenance_pause_event
    await asyncio.wait_for(event.wait(), 5)
    assert not runner._maintenance_is_paused()
    assert scheduler.try_register_running_job('expired')
    scheduler.release_running_job('expired')
    runner.maintenance_pause({'owner': OTHER, 'seconds': 60})
    runner._expire_maintenance_pause(OWNER)
    assert runner._maintenance_is_paused()
    assert runner.maintenance_status({})['owner'] == OTHER

@pytest.mark.asyncio
async def test_recovery_retains_request_until_resume(runner, monkeypatch):
    runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    assert runner._schedule_resume_pending_sessions(Platform.SLACK) == 0
    calls = []
    monkeypatch.setattr(runner, '_schedule_resume_pending_sessions', calls.append)
    runner.maintenance_resume({'owner': OWNER})
    await asyncio.sleep(0)
    assert calls == [Platform.SLACK]

@pytest.mark.macos_only
@pytest.mark.asyncio
async def test_real_socket_pause_and_resume(tmp_path, runner):
    server = GatewayControlServer(tmp_path, loop_verb_handlers={
        'maintenance_pause': runner.maintenance_pause,
        'maintenance_resume': runner.maintenance_resume,
    })
    assert await server.start()
    async def exchange(verb, **fields):
        reader, writer = await asyncio.open_unix_connection(str(server._bind_path))
        try:
            writer.write((json.dumps(dict(verb=verb, protocol=1, id=7, **fields)) + '\n').encode())
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 5))
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        response = await exchange('maintenance_pause', owner=OWNER, seconds=60)
        assert response['ok'] and response['result']['state'] == 'paused'
        assert not scheduler.try_register_running_job('socket-blocked')
        response = await exchange('maintenance_resume', owner=OWNER)
        assert response['ok'] and response['result']['state'] == 'running'
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_timer_setup_failure_never_latches(runner, monkeypatch):
    loop = asyncio.get_running_loop()
    def fail(*args, **kwargs):
        raise RuntimeError('fixture timer failure')
    with monkeypatch.context() as patch:
        patch.setattr(loop, 'call_later', fail)
        with pytest.raises(RuntimeError):
            runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    assert not runner._maintenance_is_paused()
    assert scheduler.try_register_running_job('timer-failure')
    scheduler.release_running_job('timer-failure')

@pytest.mark.asyncio
async def test_issued_queued_reservation_can_finish_during_pause(runner, monkeypatch):
    assert scheduler.try_register_running_job('issued')
    token = scheduler._running_job_admission('issued')
    executed = []
    monkeypatch.setattr(scheduler, '_run_one_job_body', lambda job, **kw: executed.append(job['id']) or True)
    try:
        runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
        assert await asyncio.to_thread(scheduler.run_one_job, {'id': 'issued'}, _admission_token=token)
        assert executed == ['issued']
        assert scheduler._running_job_admission('issued') is None
    finally:
        scheduler.release_running_job('issued')

@pytest.mark.asyncio
async def test_bare_matching_job_id_cannot_borrow_reservation(runner, monkeypatch):
    import threading
    assert scheduler.try_register_running_job('same-id')
    waiting = threading.Event()
    executed = []
    real_wait = scheduler._maintenance_resumed.wait
    def wait(timeout=None):
        waiting.set()
        return real_wait(timeout)
    monkeypatch.setattr(scheduler._maintenance_resumed, 'wait', wait)
    monkeypatch.setattr(scheduler, '_run_one_job_body', lambda job, **kw: executed.append(job['id']) or True)
    runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
    task = asyncio.create_task(asyncio.to_thread(scheduler.run_one_job, {'id': 'same-id'}))
    try:
        assert await asyncio.to_thread(waiting.wait, 5)
        assert executed == []
    finally:
        runner.maintenance_resume({'owner': OWNER})
        await asyncio.wait_for(task, 5)
        scheduler.release_running_job('same-id')
    assert executed == ['same-id']


@pytest.mark.asyncio
async def test_initial_claim_validation_is_visible(runner, monkeypatch):
    import threading
    entered = threading.Event()
    release = threading.Event()
    def heartbeat(job_id, **kwargs):
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(scheduler, 'heartbeat_fire_claim', heartbeat)
    monkeypatch.setattr(scheduler, '_run_one_job_body', lambda *a, **kw: True)
    task = asyncio.create_task(asyncio.to_thread(scheduler.run_one_job,
        {'id': 'validating', 'fire_claim': {'by': 'test-owner'}}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert 'validating' in scheduler.get_running_job_ids()
    finally:
        release.set()
        assert await asyncio.wait_for(task, 5)
    assert 'validating' not in scheduler.get_running_job_ids()


@pytest.mark.asyncio
async def test_admitted_tick_transfers_reservation_after_pause(runner, monkeypatch):
    import threading
    entered = threading.Event()
    proceed = threading.Event()
    results = []
    def batch(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        results.append(scheduler.try_register_running_job('batch-job'))
        return 1
    monkeypatch.setattr(scheduler, '_tick_body', batch)
    task = asyncio.create_task(asyncio.to_thread(scheduler.tick))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert scheduler.get_running_job_ids() == frozenset()
        receipt = runner.maintenance_pause({'owner': OWNER, 'seconds': 60})
        assert receipt['cron_admissions']['count'] >= 1
        assert receipt['shutdown_ready'] is False
        assert scheduler.tick() == 0  # new batch cannot consume due schedules
        proceed.set()
        assert await asyncio.wait_for(task, 5) == 1
        assert results == [True]
        assert scheduler._running_job_admission('batch-job') is not None
    finally:
        proceed.set()
        await asyncio.wait_for(task, 5)
        scheduler.release_running_job('batch-job')
    assert scheduler.get_running_admission_count() == 0


def test_registration_resolution_failure_leaves_no_ghost(monkeypatch):
    class BrokenHome:
        def resolve(self):
            raise OSError('fixture inaccessible profile')
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: BrokenHome())
    with pytest.raises(OSError):
        scheduler.try_register_running_job('ghost')
    assert 'ghost' not in scheduler.get_running_job_ids()
    assert 'ghost' not in scheduler._running_admissions
