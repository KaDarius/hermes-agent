# Gated fleet maintenance components

These components support the Beta daily-command-hub installation workflow. They do not install or restart anything on import, and no CLI activation command is enabled. The default live authority refuses.

`hermes_live_adapter.NativeMarkerStore` delegates owned publication/removal to the native gateway protocol and retains durable receipts. Unconditional replacement is disabled. Its caller must hold actual writer exclusions and revalidate protocol adoption; fixture callbacks do not prove that authority.

`DirectControlReader` is a strict read-only direct Unix socket client. `adapt_native_observation` validates identity, independent counter/drain freshness and exact marker generation. Neither creates admission-route holds, proves kernel peer PID, or attests loaded bytecode. A reported Git SHA is not loaded-source proof.

`hermes_job_install` preserves the target job and unrelated scheduler progress in guarded forward/rollback transactions. Its caller must supply an accepted native jobs backend and a real idle/exclusion capability. `hermes_maintenance_controller.LiveAuthority` remains unavailable until that real authority is implemented and verified. Never replace it with a no-op in live use.

Prerequisites before Beta activation: a bounded owner-coordinated maintenance window, complete writer/dispatcher exclusions, reviewed loaded source and fresh observations, approved unattended credential mapping verified in the actual child, and a graceful lifecycle/rollback plan. No ledger replay, mirror reconciliation, publishing or fleet-wide rollout is authorized by these modules.

## Native admission pause

New gateways expose `maintenance_pause`, `maintenance_status`, and
`maintenance_resume` on their local control socket. These handlers execute on
the owning event loop. Requests use protocol `1`, an integer `id`, and a caller
created 32-character lowercase hexadecimal `owner`. Pause accepts an integer
`seconds` from 1 through 600 (default 60). Resume requires the same owner.
Repeated pause requests from that owner do not extend the original lease.
Unsupported/invalid requests do not trigger signals or a restart fallback.

For example, the JSON request envelope is:

```json
{"protocol":1,"id":1,"verb":"maintenance_pause","owner":"0123456789abcdef0123456789abcdef","seconds":60}
```

Generate a new owner for each real operation; the example owner is not an
operation identifier to reuse. Retain it for the matching resume request.

This pause gates **new enrolled native admissions**, not all fleet writers.
Messaging and internal new-turn requests wait for resume; startup recovery is
retained for retry. API reservations are refused with a retryable response.
Cron batches already admitted before the pause retain their actual per-job
reservations and may finish. New batches cannot consume due schedules. A
previously claimed external fire arriving without a native reservation defers
execution until resume while keeping its owned claim heartbeat alive. Pending
batches/fires remain visible in strict maintenance counts.

The response reports `scope: native_admissions`, pending cron admission counts,
and `shutdown_ready: false`. A pause acknowledgment does not mean idle. The
pause never calls stop, interrupts agents, cancels existing work, publishes,
or changes jobs-store configuration. Existing shutdown and external-drain
states survive resume. Normal lease expiry releases only its owner; if the
registration lock cannot be acquired, release retries without clearing just
one side of the pause. Status is not a guarantee that a stalled event loop can
meet a deadline.

The old gateway cannot acquire these controls by changing files on disk.
External processes, arbitrary manual writers, runtime patchers, jobs-store
writers and marker writers still require independent adoption/exclusion.
`LiveAuthority` remains refusing until that complete authority exists. This
native pause alone must never be used to authorize a hot source replacement,
forced shutdown, ledger recovery or fleet activation.
