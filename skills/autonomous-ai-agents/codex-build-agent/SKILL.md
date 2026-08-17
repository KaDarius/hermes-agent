---
name: codex-build-agent
description: "Spawn Codex CLI as fleet build agent for code changes & CI."
version: 0.1.0
author: KaDarius (KD), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [codex, build-agent, delegation, autonomous-ai-agents]
    related_skills: [claude-code, codex, hermes-agent]
---

# Codex Build Agent Skill

Delegate implementation work to the OpenAI Codex CLI (Dash's platform) via the `terminal` tool. This skill provides a standardized interface for spawning Codex as a build/review/triage agent with proper fleet identity, commit conventions, and bus integration. It does not own delivery, Jira governance, or dispatch authority — those stay with their owners.

## When to Use

- Need heavy multi-file refactors, cloud PRs, CI/CD debugging, or security scans
- Want Codex 5.6 Sol orchestration + 5.5/5.4 execution tiers for complex builds
- Task fits Dash's lane: implementation/refactor builder, not UI/Command Hub paths (those are Forge)
- Prefer a skill that handles fleet identity (`codex:` commits, `codex-<task>` bus slug) automatically

## Don't Use For

- UI/Command Hub write-paths → use Forge (`forge:`)
- Delivery authority decisions → Cortex owns delivery
- Jira governance → Rain owns KDTSK board
- Always-on dispatch/control-plane → Hermes owns this

## Prerequisites

- OpenAI Codex CLI installed and authenticated (`codex --version` works)
- Global config at `~/.Codex/AGENTS.md` (machine-local, like Cortex's `~/.claude/CLAUDE.md`)
- Fleet identity file: `06_Metadata/Auto-Memory/codex-identity.md` (Dash persona)
- Optional: `sol-orchestrator` skill for high-level planning/routing/acceptance behind Dash's listener

## How to Run

Run Codex through the `terminal` tool, or delegate to it via `delegate_task`:

```bash
codex exec "Refactor the auth module to use the new token resolver" --cwd /path/to/repo
```

For fleet work, prefer `delegate_task(goal=..., context=..., role="leaf")`.

## Quick Reference

| Action | Command |
|---|---|
| Execute task | `codex exec "<prompt>" --cwd <path>` |
| Start interactive | `codex` |
| Check auth | `codex auth status` |
| View config | `read_file` on `~/.Codex/AGENTS.md` |

## Procedure

### 1. Validate Prerequisites
```bash
codex --version
codex auth status
test -f ~/.Codex/AGENTS.md && echo "OK" || echo "MISSING: ~/.Codex/AGENTS.md"
```

### 2. Prepare Task Context
Build a self-contained context block including:
- Repository path and branch
- Exact goal (what "done" looks like)
- Files to modify / constraints
- Fleet identity reminders: commit prefix `codex:`, bus slug `codex-<task>`
- Any required skills (e.g., `sol-orchestrator` for complex planning)

### 3. Spawn Codex Agent
Use `delegate_task` with:
- `goal`: The implementation objective
- `context`: Full context from step 2
- `role`: `leaf` (Codex cannot delegate further)
- Optionally `output_schema` for structured results

### 4. Verify Output
- Check commit(s) use `codex:` prefix
- Verify bus slug `codex-<task>` was announced
- Run CI/security gates (CX01 for creds/IO scripts >20 lines)
- Confirm Auditor gate (JV30) on delegated output if applicable

### 5. Close Loop
- Log to Session-Ledger: `fleet/codex-<task> · 🟢done`
- Update any relevant dashboard/card
- Notify Cortex if delivery decision needed

## Pitfalls

- **Machine-local config**: `~/.Codex/AGENTS.md` must be reproduced on each machine (like Cortex's CLAUDE.md)
- **Model availability**: Codex 5.6 Sol / 5.5 / 5.4 tiers must be verified at runtime
- **Not a second autonomous persona**: The listener is deterministic transport; Sol orchestrates, Codex executes
- **Shared index writes**: Cortex (PRIME) single-writes shared indexes when holding a live claim — defer, don't clobber
- **Hard gates apply**: CX01 security scan, Auditor gate (JV30), never edit generated CLAUDE.md/AGENTS.md directly

## Verification

- [ ] `codex --version` returns 5.x
- [ ] `codex auth status` shows authenticated
- [ ] `~/.Codex/AGENTS.md` exists and references fleet identity
- [ ] Test delegation produces `codex:` commit with proper bus slug
- [ ] CI passes on test PR
- [ ] Session-Ledger entry created
