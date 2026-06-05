# Distributed Coordination

Distributed coordination lets multiple app instances share one coordination state and make one machine visibly responsible for orchestration.

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

## Coordination Backends

Chat Orchestrate supports two coordination backends:

- `file`: every machine reads and writes the same JSON state file.
- `http`: every machine talks to a small coordinator service over HTTP.

Use `file` when everyone can access the same filesystem path through a LAN share, mounted drive, local VPN, or colocated deployment. Use `http` when laptops are on mobile data, different home networks, or any setup where a common file path is awkward.

## Shared File Mode

Every machine must point at the same coordination state:

```env
COORDINATION_BACKEND=file
MACHINE_ID=machine-a
AGENT_BACKENDS=codex,claude-code,simulated
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
COORDINATION_STATE_PATH=\\shared\chat-orchestrate\coordination_state.json
```

`COORDINATION_TOKEN` is hashed into the shared state. Machines with a different token or cluster ID are rejected.

## HTTP Coordinator Mode

Run one coordinator process somewhere the group can reach. That can be one laptop exposed through a private tunnel, a VPN host, or a small VPS.

Windows:

```powershell
.\scripts\run_coordinator.ps1 -HostName 0.0.0.0 -Port 8765 -ClusterId friends-project -Token "share-this-out-of-band"
```

macOS/Linux:

```sh
./scripts/run_coordinator.sh --host 0.0.0.0 --port 8765 --cluster-id friends-project --token "share-this-out-of-band"
```

Every participating UI or worker then uses:

```env
COORDINATION_BACKEND=http
COORDINATION_HTTP_URL=https://your-coordinator-url.example
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
MACHINE_ID=machine-a
AGENT_BACKENDS=codex,claude-code,simulated
```

The coordinator requires a bearer token when `COORDINATION_TOKEN` is set. Keep the token out of git and share it out of band.

For production-grade coordination, replace the JSON persistence layer behind the coordinator with Redis, Postgres, or another store that supports locks and atomic updates.

## Backend Mixes

- Codex and Claude Code on the same machine: `AGENT_BACKENDS=codex,claude-code`
- Codex on one machine, Claude Code on another: each worker points to the same file state or HTTP coordinator
- Multiple Codex workers across many machines: give each worker a unique `MACHINE_ID` and `AGENT_BACKENDS=codex`

```powershell
.\scripts\run_worker.ps1 -MachineId codex-a -Backends codex
.\scripts\run_worker.ps1 -MachineId claude-a -Backends claude-code
```

To let a worker call installed local CLIs, set:

```env
WORKER_DRY_RUN=false
```

The current command adapters are intentionally minimal and should be treated as experimental until approval gates and per-task sandboxing are added.

## Project Workspace Choice

Coordination state only decides which machine gets which task. Project space mode decides how code is laid out:

- `worktree`: multiple agents work on the same repository with separate branches.
- `clone`: agents work on separate copies or competing versions of the same repository.
- `local`: a plain folder is used without git workspace management.

Use worktrees when everyone is contributing to one shared project history. Use clones when you want separate experiments that may never merge.
