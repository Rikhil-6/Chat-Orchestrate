# Deployment

## Local Deployment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
python scripts/run_local.py
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
  src/
  docs/
  tests/
```

For deployment, set environment variables instead of committing `.env`.

## Suggested Production Shape

- Chainlit service: this app.
- OpenSwarm service: separate `swarm-api` container or process.
- Workspace volume: persistent volume mounted at `WORKSPACES_ROOT`.
- State volume: persistent volume containing `WORKSPACE_STATE_PATH` and `COORDINATION_STATE_PATH`.
- Secrets: deployment platform secret manager.

## Multi-Machine Deployment

Run one instance per machine and give each a stable identity:

```env
MACHINE_ID=machine-a
AGENT_BACKENDS=codex,claude-code,simulated
CLUSTER_ID=friends-project
COORDINATION_TOKEN=share-this-out-of-band
COORDINATION_STATE_PATH=\\shared\chat-orchestrate\coordination_state.json
WORKSPACE_STATE_PATH=\\shared\chat-orchestrate\workspace_state.json
```

Use `/claim-orchestrator` on the machine you want to lead. If no live claim exists, the app elects the first online machine by ID. Use `/machines` to see current status and `/tasks` to inspect delegated work.

Worker-only machines can run:

```powershell
python scripts/run_worker.py --machine-id machine-b --backends codex
```

## Guardrails To Add Before Broad Team Use

- Authentication in front of Chainlit.
- Per-user or per-team project-space authorization.
- Server-side command audit log.
- GitHub App integration for branch creation and PR creation.
- Approval gates before any agent can run mutating shell or Git commands.
