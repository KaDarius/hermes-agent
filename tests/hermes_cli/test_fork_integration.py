"""Preserve fork controls when integrating newer native CLI behavior."""
from argparse import ArgumentParser, Namespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('env_prompt,expected', [(None, 'configured\n\nskill content'), ('override', 'override\n\nskill content')])
def test_oneshot_keeps_configured_prompt_and_preloaded_skills(monkeypatch, env_prompt, expected):
    from hermes_cli import config, mcp_startup, oneshot, runtime_provider
    import run_agent

    monkeypatch.setattr(config, 'load_config', lambda: {'agent': {'system_prompt': 'configured'}, 'model': {'default': 'test-model'}})
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', lambda **kw: {'provider': 'test', 'api_key': 'inert-test-key'})
    monkeypatch.setattr(mcp_startup, 'ensure_mcp_discovery_before_agent_build', lambda **kw: None)
    monkeypatch.setattr(oneshot, '_build_preloaded_skills_prompt', lambda skills: 'skill content')
    monkeypatch.setattr(oneshot, '_create_session_db_for_oneshot', lambda: None)
    if env_prompt is None:
        monkeypatch.delenv('HERMES_EPHEMERAL_SYSTEM_PROMPT', raising=False)
    else:
        monkeypatch.setenv('HERMES_EPHEMERAL_SYSTEM_PROMPT', env_prompt)
    captured = {}

    class Constructed(Exception):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise Constructed

    monkeypatch.setattr(run_agent, 'AIAgent', capture)
    with pytest.raises(Constructed):
        oneshot._run_agent('hello', skills=['example'], use_config_toolsets=False)
    assert captured['ephemeral_system_prompt'] == expected


@pytest.mark.parametrize('refused', [True, False])
def test_cron_resume_guard_preserves_native_rearm_options(monkeypatch, refused):
    from hermes_cli import cron
    args = Namespace(cron_command='resume', job_id='example', run_at=None, run_now=True, force_file_write=False)
    guard = Mock(return_value=refused)
    resume = Mock(return_value=0)
    monkeypatch.setattr(cron, '_refuse_if_live_scheduler_owns_state', guard)
    monkeypatch.setattr(cron, 'cron_resume', resume)
    assert cron.cron_command(args) == (1 if refused else 0)
    guard.assert_called_once_with(False)
    if refused:
        resume.assert_not_called()
    else:
        resume.assert_called_once_with(args)


def test_cron_parser_preserves_new_options_and_explicit_guard_override():
    from hermes_cli.subcommands.cron import build_cron_parser
    parser = ArgumentParser()
    build_cron_parser(parser.add_subparsers(), cmd_cron=lambda args: None)
    create = parser.parse_args(['cron', 'create', 'every 1h', 'check', '--reasoning-effort', 'low', '--continuity', '--force-file-write'])
    assert create.reasoning_effort == 'low' and create.continuity and create.force_file_write
    resume = parser.parse_args(['cron', 'resume', 'example', '--run-now', '--force-file-write'])
    assert resume.run_now and resume.force_file_write
