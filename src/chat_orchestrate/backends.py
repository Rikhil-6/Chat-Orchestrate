from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
    installed_app: str | None = None
    app_launch_id: str | None = None


def detect_agent_backends(configured: list[str], command_overrides: dict[str, str] | None = None) -> list[str]:
    requested = [item.lower() for item in configured]
    if not requested or requested == ["auto"]:
        detected = [SIMULATED_BACKEND]
        if command_for_backend(CODEX_BACKEND, command_overrides):
            detected.append(CODEX_BACKEND)
        if command_for_backend(CLAUDE_CODE_BACKEND, command_overrides):
            detected.append(CLAUDE_CODE_BACKEND)
        return detected

    backends = []
    for backend in requested:
        if backend == "auto":
            backends.extend(detect_agent_backends(["auto"], command_overrides))
        elif backend not in backends:
            backends.append(backend)
    return backends or [SIMULATED_BACKEND]


def backend_availability(
    backends: list[str],
    command_overrides: dict[str, str] | None = None,
) -> list[BackendAvailability]:
    return [
        BackendAvailability(
            name=backend,
            available=_is_available(backend, command_overrides),
            command=command_for_backend(backend, command_overrides),
            installed_app=installed_app_for_backend(backend),
            app_launch_id=app_launch_id_for_backend(backend),
        )
        for backend in backends
    ]


def run_task(
    task: DelegatedTask,
    dry_run: bool = True,
    command_overrides: dict[str, str] | None = None,
) -> str:
    if dry_run or task.preferred_backend == SIMULATED_BACKEND:
        return (
            f"{task.preferred_backend} worker completed a preview pass for `{task.role}`.\n\n"
            f"Task: {task.title}\n"
            f"Goal: {task.goal}\n\n"
            "To use a real local agent, select `codex` or `claude-code` in the UI sidebar and make sure "
            "`WORKER_DRY_RUN=false` for worker-only processes."
        )

    command = command_for_backend(task.preferred_backend, command_overrides)
    if command is None:
        return (
            f"Backend `{task.preferred_backend}` is not executable on this machine.\n\n"
            "Restart the app from a terminal where the matching CLI is on PATH, or select another Local Agent."
        )

    prompt = (
        f"You are the `{task.role}` agent for a distributed project run.\n\n"
        f"Task: {task.title}\n"
        f"Project space: {task.project}\n"
        f"Goal:\n{task.goal}\n\n"
        "Return a concrete, useful result for your assigned role. If this is implementation work, describe "
        "the exact files, commands, or code changes you would make or have made."
    )

    if task.preferred_backend == CODEX_BACKEND:
        args = [command, "exec", "--skip-git-repo-check", prompt]
    elif task.preferred_backend == CLAUDE_CODE_BACKEND:
        args = [command, "-p", prompt]
    else:
        return f"Backend `{task.preferred_backend}` has no command runner yet."

    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=180)
    output = result.stdout.strip() or result.stderr.strip()
    return output or f"`{task.preferred_backend}` exited with code {result.returncode}."


def _is_available(backend: str, command_overrides: dict[str, str] | None = None) -> bool:
    return backend == SIMULATED_BACKEND or command_for_backend(backend, command_overrides) is not None


def command_for_backend(backend: str, command_overrides: dict[str, str] | None = None) -> str | None:
    override = _clean_command_value((command_overrides or {}).get(backend, ""))
    if override:
        if _looks_like_path(override):
            return override if Path(override).exists() else None
        return shutil.which(override)
    discovered = _discover_command(_default_command_names(backend))
    if discovered:
        return discovered
    if backend == CODEX_BACKEND:
        return _safe_which("codex")
    if backend == CLAUDE_CODE_BACKEND:
        return _safe_which("claude")
    return None


def installed_app_for_backend(backend: str) -> str | None:
    if backend == CODEX_BACKEND:
        install = _windows_codex_install()
        if install is not None:
            return f"OpenAI Codex desktop app at {install}"
    return None


def app_launch_id_for_backend(backend: str) -> str | None:
    if backend == CODEX_BACKEND and _windows_codex_install() is not None:
        return "OpenAI.Codex_2p2nqsd0c76g0!App"
    return None


def launch_backend_app(backend: str) -> bool:
    launch_id = app_launch_id_for_backend(backend)
    if not launch_id:
        return False
    try:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{launch_id}"])
    except OSError:
        return False
    return True


def backend_execution_hint(backend: str) -> str:
    app = installed_app_for_backend(backend)
    if backend == CODEX_BACKEND and app:
        return (
            "Codex desktop app is installed, but this Chainlit process still needs a callable headless "
            "CLI/API to run it as an agent. Use Launch Codex App to sign in, then restart or expose a "
            "working Codex CLI command."
        )
    return "Install the CLI or restart Chainlit from a terminal where the command is on PATH."


def _clean_command_value(value: str | None) -> str:
    clean = str(value or "").strip().strip('"').strip("'")
    return "" if clean.lower() in {"none", "null", "undefined"} else clean


def _default_command_names(backend: str) -> list[str]:
    if backend == CODEX_BACKEND:
        return ["codex", "codex.exe", "codex.cmd"]
    if backend == CLAUDE_CODE_BACKEND:
        return ["claude", "claude.exe", "claude.cmd"]
    return []


def _discover_command(names: list[str]) -> str | None:
    for name in names:
        found = _safe_which(name)
        if found and not _is_desktop_app_resource(found):
            return found

    for directory in _candidate_command_dirs():
        for name in names:
            path = directory / name
            if path.exists() and not _is_desktop_app_resource(path):
                return str(path)

    return None


def _safe_which(name: str) -> str | None:
    found = shutil.which(name)
    if not found or _is_desktop_app_resource(found):
        return None
    return found


def _candidate_command_dirs() -> list[Path]:
    candidates = []
    for key in ["APPDATA", "LOCALAPPDATA", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)"]:
        value = os.environ.get(key)
        if not value:
            continue
        base = Path(value)
        candidates.extend(
            [
                base / "npm",
                base / ".npm-global" / "bin",
                base / ".local" / "bin",
                base / "Programs",
            ]
        )

    return candidates


def _windows_codex_install() -> Path | None:
    if platform.system().lower() != "windows":
        return None
    program_files = os.environ.get("ProgramFiles")
    if not program_files:
        return None
    windows_apps = Path(program_files) / "WindowsApps"
    if not windows_apps.exists():
        return None
    installs = sorted(windows_apps.glob("OpenAI.Codex_*"), reverse=True)
    return installs[0] if installs else None


def _is_desktop_app_resource(path: str | Path) -> bool:
    normalized = str(path).lower().replace("/", "\\")
    return "\\windowsapps\\openai.codex_" in normalized and "\\app\\resources\\" in normalized


def _looks_like_path(value: str) -> bool:
    return any(marker in value for marker in ("\\", "/", ":"))
