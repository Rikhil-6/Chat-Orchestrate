# File Layout

Chat Orchestrate keeps harness source, local runtime state, and generated project workspaces separate.

## Harness Source

These folders are intended to be committed:

```text
src/chat_orchestrate/     Python app, coordination, agent bridge, workspace logic
public/                   Chainlit custom CSS and dashboard component
scripts/                  Setup, launch, coordinator, worker, and preview scripts
docs/                     Architecture and operator notes
tests/                    Unit and smoke coverage
```

## Local Runtime

These files are machine-local and ignored:

```text
.tmp/                     Launcher logs, restart markers, smoke-test output
.files/                   Chainlit uploads/runtime files
coordination_state.json   File-backed machine/task registry
runtime_config.json       Local coordinator URL/token overrides
ui_state.json             Local sidebar settings, credentials, chat history
workspace_state.json      Local project-space registry
```

Do not commit these. A fresh clone should start without them.

## Project Workspaces

Generated project code belongs under:

```text
workspaces/<project-name>/
  frontend/
    index.html
    app.js
    styles.css
  backend/
    app.py
  README.generated.md
```

The preview script assumes this `frontend/` + `backend/` shape. Older generated folders such as `server/` or `public/` inside a workspace are legacy artifacts unless a real agent explicitly creates them for the project.

The harness may create internal files under:

```text
workspaces/<project-name>/.chat-orchestrate/
```

That directory is runtime plumbing for local agent execution, not project source. Artifact scanning ignores it.

## Generated Root Folders

Root-level generated app folders such as `frontend/` are ignored. If an agent creates project code at repo root, move or regenerate it under `workspaces/<project-name>/` so the harness and preview tools can reason about it consistently.

## Codex CLI Notes

For Codex, the harness runs `codex exec` with:

```text
--sandbox workspace-write
--cd <workspace>
--output-last-message <workspace>/.chat-orchestrate/codex-final-<id>.md
```

The final-message file lets the harness read the model's final answer even when stdout is noisy or empty. If Codex exits before that file appears, the error is usually outside the project layout, such as a login issue or a locked/readonly `C:\Users\<you>\.codex` state database.

In the running UI, use `/layout` to print the active checkout, active workspace, runtime folder, current artifacts, and preview command.
