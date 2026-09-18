"""Shared parser helpers used across multiple CLI subcommand builders.

These were module-level helpers in ``hermes_cli/main.py``. They are pulled
into a neutral module so both ``main.py`` and every
``hermes_cli/subcommands/<group>.py`` builder can import them without an
import cycle. ``main.py`` re-exports them for backwards compatibility, so
existing references keep working.
"""

from __future__ import annotations

import argparse


def add_accept_hooks_flag(parser: argparse.ArgumentParser) -> None:
    """Attach the ``--accept-hooks`` flag.

    Shared across every agent subparser so the flag works regardless of CLI
    position.
    """
    parser.add_argument(
        "--accept-hooks",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Auto-approve unseen shell hooks without a TTY prompt "
            "(equivalent to HERMES_ACCEPT_HOOKS=1 / hooks_auto_accept: true)."
        ),
    )


def add_force_file_write_flag(parser: argparse.ArgumentParser) -> None:
    """Attach the ``--force-file-write`` flag to a cron-mutation subparser.

    Bypasses the KDTSK-1793 live-scheduler-owner guard (``hermes_cli.cron.
    _refuse_if_live_scheduler_owns_state``): when a live Hermes ``serve`` or
    ``gateway run`` process owns the in-memory cron job state, it flushes
    that state back over ``jobs.json`` periodically, silently reverting a
    file-only CLI mutation. This flag proceeds anyway, with a warning.
    """
    parser.add_argument(
        "--force-file-write",
        dest="force_file_write",
        action="store_true",
        default=False,
        help=(
            "Write the change to jobs.json even though a live Hermes "
            "serve/gateway process owns cron state and may silently revert "
            "it on its next flush (KDTSK-1793)."
        ),
    )
