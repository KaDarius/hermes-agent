# Gated fleet maintenance components

These components support the Beta daily-command-hub installation workflow. They do not install or restart anything on import, and no CLI activation command is enabled. The default live authority refuses.

`hermes_live_adapter.NativeMarkerStore` delegates owned publication/removal to the native gateway protocol and retains durable receipts. Unconditional replacement is disabled. Its caller must hold actual writer exclusions and revalidate protocol adoption; fixture callbacks do not prove that authority.

`DirectControlReader` is a strict read-only direct Unix socket client. `adapt_native_observation` validates identity, independent counter/drain freshness and exact marker generation. Neither creates admission-route holds, proves kernel peer PID, or attests loaded bytecode. A reported Git SHA is not loaded-source proof.

`hermes_job_install` preserves the target job and unrelated scheduler progress in guarded forward/rollback transactions. Its caller must supply an accepted native jobs backend and a real idle/exclusion capability. `hermes_maintenance_controller.LiveAuthority` remains unavailable until that real authority is implemented and verified. Never replace it with a no-op in live use.

Prerequisites before Beta activation: a bounded owner-coordinated maintenance window, complete writer/dispatcher exclusions, reviewed loaded source and fresh observations, approved unattended credential mapping verified in the actual child, and a graceful lifecycle/rollback plan. No ledger replay, mirror reconciliation, publishing or fleet-wide rollout is authorized by these modules.
