"""Tests for the KDTSK-1927 fix: the interactive console's ``/cron`` mutation
verbs (create/add, edit, pause/resume/run/remove) now route through the same
``_refuse_if_live_scheduler_owns_state`` guard as the CLI (KDTSK-1793).

The console (``hermes console`` / ``run_console_repl``) is a separate OS
process from the live scheduler (``hermes serve``) — confirmed by reading
``console_engine.py`` (no socket/IPC/shared-state connection) and
``_handle_cron_command`` (imports ``tools.cronjob_tools.cronjob`` directly,
the same low-level function the CLI uses). It is a PEER path, not an owner
path, so it has the identical silent-revert exposure the CLI guard already
closes.

This does not re-test the guard's own detection logic (already covered by
``tests/hermes_cli/test_cron.py::TestLiveSchedulerOwnerGuard``) — only that
each of the three call sites in the console handler (create/add, edit, and
the shared pause/resume/run/remove block) actually invokes it.
"""

from hermes_cli import cron as cron_cli
from hermes_cli.cli_commands_mixin import CLICommandsMixin
from cron.jobs import create_job, get_job

FAKE_OWNERS = [(4242, "python -m hermes_cli.main serve --host 127.0.0.1 --port 0")]


class _Stub(CLICommandsMixin):
    """_handle_cron_command uses no instance state — a bare stub suffices."""


def _console(monkeypatch, tmp_path, owners):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(cron_cli, "_detect_live_cron_scheduler_owners", lambda: owners)
    return _Stub()


class TestConsoleCronGuard:
    def test_create_refused_when_live_scheduler_owns_state(self, tmp_path, capsys, monkeypatch):
        console = _console(monkeypatch, tmp_path, FAKE_OWNERS)
        console._handle_cron_command('/cron add "every 1h" "Ping"')
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert "pid 4242" in out
        assert "Created job" not in out

    def test_create_bypasses_guard_with_force_file_write(self, tmp_path, capsys, monkeypatch):
        console = _console(monkeypatch, tmp_path, FAKE_OWNERS)
        console._handle_cron_command('/cron add "every 1h" "Ping" --force-file-write')
        out = capsys.readouterr().out
        assert "REFUSED" not in out
        assert "Created job" in out

    def test_create_happy_path_when_no_live_owner(self, tmp_path, capsys, monkeypatch):
        console = _console(monkeypatch, tmp_path, [])
        console._handle_cron_command('/cron add "every 1h" "Ping"')
        out = capsys.readouterr().out
        assert "REFUSED" not in out
        assert "Created job" in out

    def test_edit_refused_when_live_scheduler_owns_state(self, tmp_path, capsys, monkeypatch):
        console = _console(monkeypatch, tmp_path, [])
        job = create_job(prompt="Ping", schedule="every 1h")
        monkeypatch.setattr(cron_cli, "_detect_live_cron_scheduler_owners", lambda: FAKE_OWNERS)

        console._handle_cron_command(f'/cron edit {job["id"]} --schedule "every 2h"')
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert "Updated job" not in out
        assert get_job(job["id"])["schedule_display"] != "every 120m"

    def test_pause_refused_when_live_scheduler_owns_state(self, tmp_path, capsys, monkeypatch):
        console = _console(monkeypatch, tmp_path, [])
        job = create_job(prompt="Ping", schedule="every 1h")
        monkeypatch.setattr(cron_cli, "_detect_live_cron_scheduler_owners", lambda: FAKE_OWNERS)

        console._handle_cron_command(f'/cron pause {job["id"]}')
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert "Paused job" not in out

    def test_remove_bypasses_guard_with_force_file_write(self, tmp_path, capsys, monkeypatch):
        console = _console(monkeypatch, tmp_path, [])
        job = create_job(prompt="Ping", schedule="every 1h")
        monkeypatch.setattr(cron_cli, "_detect_live_cron_scheduler_owners", lambda: FAKE_OWNERS)

        console._handle_cron_command(f'/cron remove {job["id"]} --force-file-write')
        out = capsys.readouterr().out
        assert "REFUSED" not in out
        assert "Removed job" in out
        assert get_job(job["id"]) is None
