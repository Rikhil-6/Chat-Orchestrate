# Chat Orchestrate

Chainlit UI for steering an Open Swarm-backed group of agents inside named project spaces.

The app is designed for multiple browser clients to connect to the same deployment, pick a project workspace, and ask a coordinator agent to split work across specialist agents. Project spaces can point at local folders or Git worktrees, so the same interface can later be deployed beside GitHub-hosted work.

## What This Gives You

- A Chainlit chat UI for human-in-the-loop orchestration.
- An async orchestrator that routes work through Coordinator, Researcher, Engineer, Reviewer, and Documenter roles.
- An OpenSwarm adapter that calls a `swarm-api` OpenAI-compatible endpoint.
- A project-space registry backed by local JSON state.
- A distributed machine registry with orchestrator election and task delegation.
- Mixed backend support for Codex, Claude Code, OpenSwarm, and simulated workers.
- Git worktree helpers for isolated branch/workspace creation.
- Markdown docs for local setup, architecture, and deployment.

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
python scripts/run_local.py
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

Open Chainlit at [http://localhost:7860](http://localhost:7860).

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
/machines
/claim-orchestrator
/tasks
```

After selecting a project space, normal messages are treated as orchestration goals.

Use **worktree mode** when agents are working on the same project with separate branches. Use **clone mode** when agents should explore separate copies or competing versions of the same repository.

## Multi-Machine Coordination

Each running app instance registers a `MACHINE_ID`, capabilities, and agent backends in `COORDINATION_STATE_PATH`. If no live orchestrator is claimed, the app elects one from the online machines. The orchestrator machine is shown in the Chainlit startup message and every run summary.

For multiple machines, point every deployment at the same shared state location:

```env
MACHINE_ID=machine-a
AGENT_BACKENDS=codex,claude-code,simulated
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
COORDINATION_STATE_PATH=\\shared\chat-orchestrate\coordination_state.json
WORKSPACE_STATE_PATH=\\shared\chat-orchestrate\workspace_state.json
```

The AI delegation pass records role-specific tasks against available machines and chooses a preferred backend, such as `codex`, `claude-code`, `openswarm`, or `simulated`.

Chainlit also shows a **Machine Status** panel on startup with native buttons to refresh status, claim/release orchestrator status, and inspect recent delegated tasks.

## Worker Launch

Run UI plus one local worker:

```powershell
python scripts/run_local.py --machine-id machine-a --backends codex,claude-code,simulated
```

Run worker-only processes on additional machines:

```powershell
python scripts/run_worker.py --machine-id machine-b --backends codex
python scripts/run_worker.py --machine-id machine-c --backends claude-code
```

Workers dry-run by default. Set `WORKER_DRY_RUN=false` only when you intentionally want a worker to call local agent CLIs.

## Fresh GitHub Clone

Runtime files are ignored: `.env`, `coordination_state.json`, `workspace_state.json`, and `workspaces/`. A new clone starts clean.

For a shared swarm, give friends the same `CLUSTER_ID`, `COORDINATION_TOKEN`, and shared state location out of band. Do not commit those values.

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
  swarm_client.py       OpenSwarm API adapter and local fallback
scripts/
  run_local.py
  run_worker.py
  run_chainlit.py
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
