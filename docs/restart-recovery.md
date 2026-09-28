# Failed in-band restart recovery

An in-band restart (`/restart` or the gateway's restart signal) stops admitting
new messages while existing work finishes. `agent.restart_after_turn_timeout`
is the wait budget: an explicit finite, nonnegative value is preserved; an
unset value uses 300 seconds. Zero refuses immediately if work remains.
Invalid values are rejected rather than silently replaced.

If work fails to drain, the gateway does not launch the detached helper or
call `stop()`. It abandons that attempt and restores only the intake pause
created by it, provided the same attempt still owns the transition and no
shutdown, external drain marker, or maintenance hold intervened. Unknown hold
state preserves the pause. Recovery is evaluated once; it does not retry the
restart, replay queued work, or release another owner's hold. A later explicit
restart is a new attempt. Wedged work is not permission to interrupt it.

A restart requested while platform connections are still starting is refused
with `existing_lifecycle_transition`; it does not stop startup or schedule a
retry. Wait for startup to finish before making a new explicit restart request.
A stuck startup needs a separately reviewed operator procedure: this change
does not introduce a startup-abort mechanism.

The systemd graceful-wait timeout leaves the running service untouched.
Detached helpers refuse to run their restart command if the original process
still exists when their deadline expires. This is not a guarantee that all
other restart commands or supervisor actions are non-destructive.

## Investigation evidence

Each attempt writes privacy-limited JSON to the active profile's
`logs/restart_attempts/<attempt-id>.json` and the gateway logger. Records include
timestamps, effective budget and known source, observed outcome/reason,
remaining-work count when measured, and recovery/refusal reason. They exclude
message bodies, routing identifiers, credentials, and exception text. The root
cause is explicitly unverified: a timeout proves that the wait expired, not
why a job was slow. Per-attempt files describe the latest phase; the ordinary
logger preserves emitted transitions subject to its configured retention.
If a sink fails, available in-memory diagnostics record that failure.

New restart notification markers carry the attempt ID. A later process may
announce success only after a matching receipt says the previous stop
completed. Missing/failed diagnostics cannot authorize a success notification.
The reader atomically claims a marker before delivery and cleans only that
claimed file; another command's newer marker and the redelivery dedup record
survive. Legacy markers retain their existing notification behavior.

## Verification and limits

Behavioral tests: `tests/gateway/test_restart_recovery.py`, restart drain,
notification, redelivery, service-detection and timeout suites, plus the CLI
service suite. Run them through `scripts/run_tests.sh`. Windows-only tests need
a Windows runner; a skip on another host is not Windows verification.

A successful initial drain still enters the existing force-capable `stop()`
path and its watchdog. Counters and marker observations do not establish global
writer/admission exclusion across processes; supervisors, out-of-band stop,
force-mode CLI operations and runtime deployment require separate acceptance.
Merging source does not install or activate this behavior on any fleet host.

—Dash
