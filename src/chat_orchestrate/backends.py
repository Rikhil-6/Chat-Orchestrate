from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .models import DelegatedTask


SIMULATED_BACKEND = "simulated"
CODEX_BACKEND = "codex"
CLAUDE_CODE_BACKEND = "claude-code"
OPEN_SWARM_BACKEND = "openswarm"


@dataclass(frozen=True)
class BackendAvailability:
    name: str
    available: bool
    command: str | None = None


def detect_agent_backends(configured: list[str]) -> list[str]:
    requested = [item.lower() for item in configured]
    if not requested or requested == ["auto"]:
        detected = [SIMULATED_BACKEND]
        if shutil.which("codex"):
            detected.append(CODEX_BACKEND)
        if shutil.which("claude"):
            detected.append(CLAUDE_CODE_BACKEND)
        return detected

    backends = []
    for backend in requested:
        if backend == "auto":
            backends.extend(detect_agent_backends(["auto"]))
        elif backend not in backends:
            backends.append(backend)
    return backends or [SIMULATED_BACKEND]


def backend_availability(backends: list[str]) -> list[BackendAvailability]:
    return [BackendAvailability(name=backend, available=_is_available(backend), command=_command(backend)) for backend in backends]


def run_task(task: DelegatedTask, dry_run: bool = True) -> str:
    if dry_run or task.preferred_backend == SIMULATED_BACKEND:
        return (
            f"{task.preferred_backend} worker completed a dry-run for `{task.role}`.\n\n"
            f"Task: {task.title}\n"
            f"Goal: {task.goal}"
        )

    command = _command(task.preferred_backend)
    if command is None:
        return f"Backend `{task.preferred_backend}` is not executable on this machine."

    if task.preferred_backend == CODEX_BACKEND:
        args = [command, "exec", "--skip-git-repo-check", task.goal]
    elif task.preferred_backend == CLAUDE_CODE_BACKEND:
        args = [command, "-p", task.goal]
    else:
        return f"Backend `{task.preferred_backend}` has no command runner yet."

    result = subprocess.run(args, capture_output=True, text=True, check=False)
    output = result.stdout.strip() or result.stderr.strip()
    return output or f"`{task.preferred_backend}` exited with code {result.returncode}."


def _is_available(backend: str) -> bool:
    return backend == SIMULATED_BACKEND or _command(backend) is not None


def _command(backend: str) -> str | None:
    if backend == CODEX_BACKEND:
        return shutil.which("codex")
    if backend == CLAUDE_CODE_BACKEND:
        return shutil.which("claude")
    return None
