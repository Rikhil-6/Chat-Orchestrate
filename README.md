# Chat Orchestrate

Chainlit UI for steering an Open Swarm-backed group of agents inside named project spaces.

The app is designed for multiple browser clients to connect to the same deployment, pick a project workspace, and ask a coordinator agent to split work across specialist agents. Project spaces can point at local folders or Git worktrees, so the same interface can later be deployed beside GitHub-hosted work.

## What This Gives You

- A Chainlit chat UI for human-in-the-loop orchestration.
- An async orchestrator that routes work through Coordinator, Researcher, Engineer, Reviewer, and Documenter roles.
- An OpenSwarm adapter that calls a `swarm-api` OpenAI-compatible endpoint.
- A project-space registry backed by local JSON state.
- A distributed machine registry with orchestrator election and task delegation.
- A2A-compatible discovery/RPC endpoints on the hosted coordinator for mixed agent harnesses.
- Mixed backend support for Codex, Claude Code, OpenSwarm, and simulated workers.
- A compact cluster roster showing connected machines and their agent backends.
- Local chat routing through installed Codex or Claude Code CLIs when available.
- Git worktree helpers for isolated branch/workspace creation.
- Markdown docs for local setup, architecture, and deployment.

## Quickstart

Windows PowerShell:

```powershell
.\scripts\setup.ps1
.\scripts\run_local.ps1
```

macOS/Linux:

```sh
./scripts/setup.sh
./scripts/run_local.sh
```

By default the app runs with a deterministic local fallback client so you can test the UI without model keys. To connect OpenSwarm, set:

```env
OPEN_SWARM_BASE_URL=http://localhost:8000
OPEN_SWARM_API_KEY=your-key-if-required
OPEN_SWARM_MODEL=codey
USE_OPEN_SWARM=true
```

Then run your OpenSwarm API separately:

```powershell
swarm-api
```

Open Chainlit at [http://localhost:7862](http://localhost:7862).

Normal chat messages use locally installed agent CLIs when available. With `AGENT_BACKENDS=auto`, the app detects `codex` and `claude`; if neither is available, it falls back to the simulated preview client. Set `USE_LOCAL_AGENT_CHAT=false` to force preview mode.

The Chainlit sidebar includes a **Local Agent** selector so you can choose `codex`, `claude-code`, `openswarm`, or `simulated` without editing `.env`. The sidebar updates to show only the credential/profile fields for that selected agent. The selected backend is advertised to the cluster, and chat turns use that local profile when it is ready. Codex can use a working CLI command or a saved `OPENAI_API_KEY` / sidebar **OpenAI API Key** for Responses API fallback. Claude Code uses its local command/login profile. Credentials are saved locally in ignored `ui_state.json`, so each computer keeps its own agent harness profile without committing secrets. The app can also detect the Microsoft Store Codex desktop app and offer **Launch Codex App** for login/setup, but GUI app installation is separate from headless agent execution.

Machine capability tags are computed, not hand-authored. The coordinator infers roles from the selected local agent, detected tool readiness, the default agent set, and the current chat goal. A prompt that asks for a backend here and frontend on another machine should advertise and route `backend`/`frontend` work differently from a prompt that only asks for review or documentation.

On startup, the app opens a **Harness Dashboard** side panel with live machines, local execution policy, workspace details, and repo-consolidation flow. The chat bar remains the place to talk to the local/coordinating agent. Use `/dashboard`, the **Dashboard** action, or the floating **Dashboard / Settings** switcher to reopen panels after closing them.

Selecting an agent is separate from being ready to execute it. Codex and Claude Code should be signed in through their normal local CLI flows and launched from a terminal where `codex` or `claude` is on `PATH`, or configured with a full command path in the sidebar. A signed-in desktop app can be launched for setup, but the harness cannot reuse a GUI-only desktop session as a headless worker unless a callable CLI/API path is also available. If Codex CLI is unavailable, saving an OpenAI API key in the sidebar enables the Codex API fallback.

Use **Auto-detect Agents** or `/detect-agents` to scan PATH plus common npm, user-local, WindowsApps, Homebrew, and local bin install locations for `codex` and `claude`. If you installed a command after the app started or changed terminal PATH, use **Restart App** or `/restart-app`; when launched through `scripts/run_local.py`, the supervisor relaunches the UI and worker automatically.

## Project Spaces

Project spaces are kept under `WORKSPACES_ROOT` unless an absolute path is supplied. The default is:

```text
./workspaces
```

In chat, use commands like:

```text
/spaces
/use my-app
/create-space my-app C:\code\my-app
/worktree my-app C:\code\my-app feature/agent-pass
/clone my-app https://github.com/org/repo.git feature/experiment
/workspace-modes
/detect-agents
/restart-app
/machines
/claim-orchestrator
/tasks
/backends
/connect
/host-coordinator
/connect-coordinator
/connect-http
/connect-file
/clear-history
```

After selecting a project space, normal messages are treated as orchestration goals.

Use **worktree mode** when agents are working on the same project with separate branches. Use **clone mode** when agents should explore separate copies or competing versions of the same repository.

Distributed code still converges through one canonical project repo. In worktree mode, machines return branches such as `codex/backend-api` or `claude/frontend-ui`; in clone mode, they return patches, branches, PRs, or artifacts. Frontend machines should also return preview URLs or screenshots so backend-only machines can inspect the result before the coordinator merges everything into the selected project space.

## Multi-Machine Coordination

Each running app instance registers a `MACHINE_ID`, capabilities, and agent backends in `COORDINATION_STATE_PATH`. If no live orchestrator is claimed, the app elects one from the online machines. The orchestrator machine is shown in the Chainlit startup message and every run summary.

For machines on the same LAN, shared drive, VPN filesystem, or mounted network folder, use the default file backend and point every deployment at the same state location:

```env
COORDINATION_BACKEND=file
MACHINE_ID=machine-a
AGENT_BACKENDS=codex,claude-code,simulated
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
COORDINATION_STATE_PATH=\\shared\chat-orchestrate\coordination_state.json
WORKSPACE_STATE_PATH=\\shared\chat-orchestrate\workspace_state.json
```

For machines on mobile data or different networks, run one HTTP coordinator that everyone can reach through a VPN, tunnel, VPS, or other private URL:

```powershell
.\scripts\run_coordinator.ps1 -HostName 0.0.0.0 -Port 8765 -ClusterId friends-project -Token "share-this-out-of-band"
```

Every UI or worker then uses:

```env
COORDINATION_BACKEND=http
COORDINATION_HTTP_URL=https://your-coordinator-url.example
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
```

The AI delegation pass records role-specific tasks against available machines and chooses a preferred backend, such as `codex`, `claude-code`, `openswarm`, or `simulated`. When a goal mentions backend/frontend work, the planner can create specialist backend and frontend tasks. A connected Chainlit app also runs a lightweight local worker while it is open, so another machine can claim its assigned task and return the result through the coordinator. The main chat uses `DELEGATED_TASK_ACK_SECONDS` to wait only briefly for remote workers to claim or finish before responding; longer-running worker status continues in the dashboard.

Chainlit also shows a compact **Cluster Roster** plus a **Machine Status** panel on startup. The roster is updated as you interact with the app and shows each connected machine, status, role, agent backends, and the currently selected local chat backend. If two laptops both show `Online 1`, open `/connect`; they are probably each using their own local state file.

Use **Host Coordinator** or `/host-coordinator` on one running machine to start the shared HTTP coordinator from the UI. The UI asks for a human project/session name, turns it into a cluster id such as `google-site-a3f9`, generates a fresh token, and prints a copyable connection pack. It is the button version of:

```powershell
.\scripts\run_coordinator.ps1 -HostName 0.0.0.0 -Port 8765 -ClusterId friends-project -Token "share-this-out-of-band"
```

Use `/connect-coordinator` or the **Connect to Coordinator** button on the other machines to paste the host's connection pack. It can also accept just a coordinator URL and will ask only for missing token details. These values are saved to ignored local runtime config in `runtime_config.json`, and the current UI session switches over immediately.

When auto-host fallback is enabled, a machine that cannot reach any saved coordinator URL can start its own coordinator on `COORDINATOR_PORT`. Other machines need that fallback URL saved too, so add multiple coordinator URLs when you have more than one possible host.

If `8765` is already busy, **Host Coordinator** picks the next available port and prints that port in the connection pack. If other machines still cannot open `/health`, allow inbound TCP for that port in the host OS firewall or use a VPN/tunnel URL.

Use **End Session** or `/end-session` to stop any coordinator hosted by this UI and wipe saved coordinator URLs/tokens from `runtime_config.json`.

## Agent2Agent Interop

When a machine is hosting or connected to the HTTP coordinator, the coordinator also exposes a small A2A-compatible surface:

```text
GET  /.well-known/agent-card.json
GET  /a2a/agent-card
POST /a2a/rpc
```

The Agent Card advertises this cluster as a JSON-RPC A2A endpoint with project-orchestration and local-agent-handoff skills. The JSON-RPC endpoint currently supports `GetExtendedAgentCard`, `ListTasks`, `GetTask`, and `SendMessage`. `SendMessage` creates a delegated coordinator task in the same shared task state used by the Chainlit UI and workers.

This is intentionally an interop lane, not a replacement for the app's own coordinator. The coordinator still owns machine membership, host election, shared tokens, and repo convergence. A2A lets external or future harnesses discover the cluster and submit/check task-shaped work without needing to know whether a machine is running Codex, Claude Code, Gemini CLI, OpenSwarm, or another local agent.

Use the same bearer token shown in the host connection pack:

```http
Authorization: Bearer <coordination-token>
```

## Worker Launch

Run UI plus one local worker:

```powershell
.\scripts\run_local.ps1 -MachineId machine-a -Backends codex,claude-code,simulated
```

Run worker-only processes on additional machines:

```powershell
.\scripts\run_worker.ps1 -MachineId machine-b -Backends codex
.\scripts\run_worker.ps1 -MachineId machine-c -Backends claude-code
```

Workers dry-run by default. Set `WORKER_DRY_RUN=false` only when you intentionally want a worker-only process to call local agent CLIs. Chainlit UI sessions use the sidebar **Local Agent** selection for their built-in lightweight worker.

On macOS/Linux, use the `.sh` versions with the Python-style flags:

```sh
./scripts/run_local.sh --machine-id machine-a --backends codex,claude-code,simulated
./scripts/run_worker.sh --machine-id machine-b --backends codex
```

## Fresh GitHub Clone

Runtime files are ignored: `.env`, `runtime_config.json`, `ui_state.json`, `coordination_state.json`, `workspace_state.json`, and `workspaces/`. A new clone starts clean.

For a shared swarm, give friends the same `CLUSTER_ID`, `COORDINATION_TOKEN`, and either the shared state location or HTTP coordinator URL out of band. Do not commit those values.

You can enter the shared coordinator URL and token through the UI with `/connect-coordinator`, so friends do not have to edit `.env` for normal joining. `/connect-http` still works as an alias.

## OpenSwarm Notes

This project uses a small REST adapter instead of importing OpenSwarm internals. That keeps the Chainlit UI deployable and lets OpenSwarm evolve behind `swarm-api`. Current OpenSwarm package docs describe `open-swarm` as exposing `swarm-cli` and `swarm-api`, with custom blueprints and OpenAI-compatible API integration.

## Repo Layout

```text
src/chat_orchestrate/
  chainlit_app.py       Chainlit entrypoint
  config.py             Runtime settings
  models.py             Shared dataclasses
  orchestrator.py       Multi-agent workflow
  coordination.py       Machine registry and task delegation state
  backends.py           Codex/Claude/OpenSwarm/simulated backend detection
  worker.py             Polling worker runner
  project_space.py      Workspace and git worktree management
  swarm_client.py       OpenSwarm API adapter and local CLI fallback
  ui_state.py           Ignored local UI preferences and chat history
scripts/
  run_local.py
  run_worker.py
  run_chainlit.py
  run_coordinator.py
  setup.ps1 / setup.sh
  run_local.ps1 / run_local.sh
  run_worker.ps1 / run_worker.sh
  run_coordinator.ps1 / run_coordinator.sh
docs/
  ARCHITECTURE.md
  DEPLOYMENT.md
  PROJECT_SPACES.md
  DISTRIBUTED_COORDINATION.md
tests/
```

## Development

```powershell
ruff check .
pytest
```

## Security

Do not commit `.env`, local workspace state, or project worktrees. The app treats project paths as local operator-controlled paths and does not clone arbitrary GitHub URLs from chat by default.
