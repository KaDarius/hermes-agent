"""Native runner/status integration using real imports and temporary homes."""
import builtins
import copy
import sys
from types import ModuleType, SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway import status


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    obj = object.__new__(GatewayRunner)
    obj._running_agents = {}
    obj.adapters = {}
    obj._restart_requested = False
    return obj


def test_real_registry_registration_appears_in_native_sample(runner):
    import cron.scheduler as scheduler
    key = 'maintenance-counter-test'
    assert scheduler.try_register_running_job(key)
    try:
        runner._running_agents = {'turn': object()}
        runner.adapters[Platform.API_SERVER] = SimpleNamespace(strict_active_agent_work_count=lambda: 3)
        sample = runner._maintenance_counter_snapshot()
        assert sample['counters']['messaging'] == {'valid': True, 'count': 1}
        assert sample['counters']['cron'] == {'valid': True, 'count': len(scheduler.get_running_job_ids())}
        assert sample['counters']['api'] == {'valid': True, 'count': 3}
        assert key not in repr(sample)
    finally:
        scheduler.release_running_job(key)


@pytest.mark.parametrize('value', [True, -1, 0.5, '0', None])
def test_invalid_raw_api_count_stays_unknown(runner, value):
    runner.adapters[Platform.API_SERVER] = SimpleNamespace(strict_active_agent_work_count=lambda: value)
    assert runner._maintenance_counter_snapshot()['counters']['api'] == {'valid': False, 'count': None}


def test_failed_cron_and_absent_api_are_unknown(runner, monkeypatch):
    import cron.scheduler as scheduler
    def fail(): raise RuntimeError('private fixture error')
    monkeypatch.setattr(scheduler, 'get_running_admission_count', fail)
    sample = runner._maintenance_counter_snapshot()
    assert sample['counters']['cron'] == {'valid': False, 'count': None}
    assert sample['counters']['api'] == {'valid': False, 'count': None}
    assert 'private fixture error' not in repr(sample)


@pytest.mark.parametrize('method,args', [('_update_runtime_status', ('draining',)), ('_persist_active_agents', ())])
def test_native_write_observes_missing_registry_before_legacy_import(runner, monkeypatch, method, args):
    native_import = builtins.__import__
    imports = []
    empty = ModuleType('cron.scheduler'); empty.get_running_job_ids = lambda: frozenset()
    monkeypatch.delitem(sys.modules, 'cron.scheduler', raising=False)
    def intercept(name, *a, **kw):
        if name == 'cron.scheduler':
            imports.append(name)
            monkeypatch.setitem(sys.modules, name, empty)
            return empty
        return native_import(name, *a, **kw)
    monkeypatch.setattr(builtins, '__import__', intercept)
    getattr(runner, method)(*args)
    persisted = status.read_runtime_status()
    assert imports == ['cron.scheduler']
    assert persisted['maintenance_counters']['counters']['cron'] == {'valid': False, 'count': None}
    assert persisted['active_agents'] == 0  # retained legacy aggregate


def test_real_status_file_keeps_sample_age_and_original_writer(runner, monkeypatch):
    runner._persist_active_agents()
    before = status.read_runtime_status()['maintenance_counters']
    pid = status._build_pid_record()
    pid.update(pid=999999, start_time=111, hermes_home='/unrelated-profile')
    monkeypatch.setattr(status, '_build_pid_record', lambda: copy.deepcopy(pid))
    status.write_runtime_status(platform='fixture', platform_state='connected')
    after = status.read_runtime_status()
    assert after['pid'] == 999999
    assert after['maintenance_counters'] == before
    assert after['maintenance_counters']['writer_pid'] != after['pid']


@pytest.mark.parametrize('field,value', [
    ('_pending_agent_requests', True),
    ('_pending_agent_requests', '0'),
    ('_inflight_agent_runs', -1),
    ('_active_run_tasks', None),
])
def test_real_api_registry_corruption_is_not_valid_idle(runner, field, value):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    setattr(adapter, field, value)
    runner.adapters[Platform.API_SERVER] = adapter
    assert runner._maintenance_counter_snapshot()['counters']['api'] == {'valid': False, 'count': None}


@pytest.mark.asyncio
async def test_real_api_pending_and_task_registries_are_counted(runner):
    import asyncio
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    active = asyncio.get_running_loop().create_future()
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    adapter._pending_agent_requests = 2
    adapter._inflight_agent_runs = 3
    adapter._active_run_tasks = {'active': active, 'finished': finished}
    runner.adapters[Platform.API_SERVER] = adapter
    try:
        assert runner._maintenance_counter_snapshot()['counters']['api'] == {'valid': True, 'count': 6}
        active.set_result(None)
        assert runner._maintenance_counter_snapshot()['counters']['api'] == {'valid': True, 'count': 5}
    finally:
        active.cancel()


@pytest.mark.parametrize('result', [0, None, 'done'])
def test_invalid_task_completion_state_is_unknown(runner, result):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._active_run_tasks = {'broken': SimpleNamespace(done=lambda: result)}
    runner.adapters[Platform.API_SERVER] = adapter
    assert runner._maintenance_counter_snapshot()['counters']['api'] == {'valid': False, 'count': None}


def test_api_registry_read_error_does_not_leak_or_become_idle(runner):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    def fail():
        raise RuntimeError('private API registry detail')
    adapter._active_run_tasks = {'broken': SimpleNamespace(done=fail)}
    runner.adapters[Platform.API_SERVER] = adapter
    sample = runner._maintenance_counter_snapshot()
    assert sample['counters']['api'] == {'valid': False, 'count': None}
    assert 'private API registry detail' not in repr(sample)
    assert adapter.active_agent_work_count() == 0  # legacy callers unchanged
