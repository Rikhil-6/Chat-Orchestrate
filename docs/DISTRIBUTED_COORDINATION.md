# Distributed Coordination

Distributed coordination lets multiple app instances point at one shared state file and make one machine visibly responsible for orchestration.

## Concepts

- **Machine**: one running Chainlit app instance with a stable `MACHINE_ID`.
- **Orchestrator**: the machine that owns planning and delegation for new goals.
- **Worker**: any online machine that can receive role-specific delegated tasks.
- **Backend**: an agent runtime advertised by a machine, such as `codex`, `claude-code`, `openswarm`, or `simulated`.
- **Task**: a recorded assignment such as `engineer` or `reviewer` for a run.

## Commands

```text
/machines
/claim-orchestrator
/release-orchestrator
/tasks
/backends
/workspace-modes
```

The Chainlit app also renders a **Machine Status** panel on startup. Use its buttons to refresh machine state, claim or release orchestrator status, and inspect recent delegated tasks without typing commands.

## Election

On startup, each app sends a heartbeat. If no live orchestrator exists, the app elects the first online machine by machine ID. An explicit `/claim-orchestrator` overrides automatic election while that machine keeps heartbeating.

`ORCHESTRATOR_TTL_SECONDS` controls how long a machine can be silent before its claim is considered stale.

## Delegation

When a user sends a normal prompt:

1. The app refreshes the local machine heartbeat.
2. It resolves the active orchestrator.
3. It creates delegated tasks from the goal and available machine capabilities.
4. It sends the delegation plan into the agent context.
5. It records the run's assignments in `coordination_state.json`.
6. Workers poll for tasks assigned to their `MACHINE_ID` and matching backend.

Workers dry-run by default, which lets you test Codex and Claude Code mixes without invoking external agent CLIs.

## Shared State

Every machine must point at the same coordination state:

```env
MACHINE_ID=machine-a
AGENT_BACKENDS=codex,claude-code,simulated
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
COORDINATION_STATE_PATH=\\shared\chat-orchestrate\coordination_state.json
```

For production, replace this file with Redis, Postgres, or another store that supports locks and atomic updates.

`COORDINATION_TOKEN` is hashed into the shared state. Machines with a different token or cluster ID are rejected.

## Backend Mixes

- Codex and Claude Code on the same machine: `AGENT_BACKENDS=codex,claude-code`
- Codex on one machine, Claude Code on another: each worker points to the same `COORDINATION_STATE_PATH`
- Multiple Codex workers across many machines: give each worker a unique `MACHINE_ID` and `AGENT_BACKENDS=codex`

```powershell
python scripts/run_worker.py --machine-id codex-a --backends codex
python scripts/run_worker.py --machine-id claude-a --backends claude-code
```

To let a worker call installed local CLIs, set:

```env
WORKER_DRY_RUN=false
```

The current command adapters are intentionally minimal and should be treated as experimental until approval gates and per-task sandboxing are added.
