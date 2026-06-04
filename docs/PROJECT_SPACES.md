# Project Spaces

A project space is a named local path that agents treat as the working boundary for a goal.

## Commands

```text
/spaces
/use my-app
/create-space my-app C:\code\my-app
/worktree my-app C:\code\my-app feature/agent-pass
/clone my-app https://github.com/org/repo.git feature/experiment
/workspace-modes
```

## Local Folder

Use `/create-space` when you already have a folder:

```text
/create-space api C:\code\api
```

The folder is created if it does not exist.

## Git Worktree

Use `/worktree` when agents are working on the same project/repository and need isolated branches from one local repo:

```text
/worktree api-agent-pass C:\code\api feature/agent-pass
```

This runs the equivalent of:

```powershell
git worktree add -b feature/agent-pass <WORKSPACES_ROOT>\api-agent-pass HEAD
```

## Git Clone

Use `/clone` when agents should work on separate copies or competing versions of the same repository:

```text
/clone api-version-a https://github.com/org/api.git feature/version-a
/clone api-version-b https://github.com/org/api.git feature/version-b
```

Clone mode always clones into `WORKSPACES_ROOT`, which keeps remote repository operations inside the configured workspace boundary.

## State

Registered spaces are stored in `WORKSPACE_STATE_PATH`, defaulting to:

```text
./workspace_state.json
```

Do not commit this file unless you intentionally want shared local paths in the repository.
