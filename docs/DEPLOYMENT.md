# Deployment

## Local Deployment

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

Run OpenSwarm separately when `USE_OPEN_SWARM=true`:

```powershell
pip install open-swarm
swarm-api
```

With this split, Chainlit runs on `7860` and OpenSwarm can keep `8000`.

## GitHub-Friendly Folder Deployment

This repo is structured so it can live under a larger GitHub repository folder. Keep these files together:

```text
chat-orchestrate/
  README.md
  pyproject.toml
  scripts/
  src/
  docs/
  tests/
```

For deployment, set environment variables instead of committing `.env`.

## Suggested Production Shape

- Chainlit service: this app.
- Optional coordinator service: `scripts/run_coordinator.py` exposed only to trusted participants.
- OpenSwarm service: separate `swarm-api` container or process.
- Workspace volume: persistent volume mounted at `WORKSPACES_ROOT`.
- State volume: persistent volume containing `WORKSPACE_STATE_PATH` and `COORDINATION_STATE_PATH`.
- Secrets: deployment platform secret manager.

## Multi-Machine Deployment

Run one instance per machine and give each a stable identity.

Use the file backend when every machine can reach the same shared path:

```env
COORDINATION_BACKEND=file
MACHINE_ID=machine-a
AGENT_BACKENDS=codex,claude-code,simulated
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
COORDINATION_STATE_PATH=\\shared\chat-orchestrate\coordination_state.json
WORKSPACE_STATE_PATH=\\shared\chat-orchestrate\workspace_state.json
```

Use the HTTP backend when machines are on different networks:

```powershell
.\scripts\run_coordinator.ps1 -HostName 0.0.0.0 -Port 8765 -ClusterId friends-project -Token "share-this-out-of-band"
```

```env
COORDINATION_BACKEND=http
COORDINATION_HTTP_URL=https://your-coordinator-url.example
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
```

Use `/claim-orchestrator` on the machine you want to lead. If no live claim exists, the app elects the first online machine by ID. Use `/machines` to see current status and `/tasks` to inspect delegated work.

Worker-only machines can run:

```powershell
.\scripts\run_worker.ps1 -MachineId machine-b -Backends codex
```

```sh
./scripts/run_worker.sh --machine-id machine-b --backends codex
```

## Guardrails To Add Before Broad Team Use

- Authentication in front of Chainlit.
- Per-user or per-team project-space authorization.
- Server-side command audit log.
- GitHub App integration for branch creation and PR creation.
- Approval gates before any agent can run mutating shell or Git commands.
