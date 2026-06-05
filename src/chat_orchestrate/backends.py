from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

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
    openai_api_key: str = "",
    codex_api_model: str = "gpt-5.3-codex",
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
        if task.preferred_backend == CODEX_BACKEND and openai_api_key.strip():
            return run_codex_api_task(task, openai_api_key.strip(), codex_api_model)
        return (
            f"Backend `{task.preferred_backend}` is not executable on this machine.\n\n"
            f"{backend_execution_hint(task.preferred_backend)}"
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


def run_codex_api_task(task: DelegatedTask, api_key: str, model: str) -> str:
    prompt = (
        f"You are the `{task.role}` agent for a distributed project run.\n\n"
        f"Task: {task.title}\n"
        f"Project space: {task.project}\n"
        f"Goal:\n{task.goal}\n\n"
        "Return a concrete, useful result for your assigned role."
    )
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": prompt},
            timeout=180,
        )
    except httpx.HTTPError as exc:
        return f"OpenAI API request failed: {exc}"
    if response.status_code >= 400:
        return f"OpenAI API request failed: HTTP {response.status_code} {response.text}"
    return extract_response_text(response.json())


def extract_response_text(payload: dict) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts).strip() or "No text output returned by the OpenAI API."


def _is_available(backend: str, command_overrides: dict[str, str] | None = None) -> bool:
    return backend == SIMULATED_BACKEND or command_for_backend(backend, command_overrides) is not None


def command_for_backend(backend: str, command_overrides: dict[str, str] | None = None) -> str | None:
    override = _clean_command_value((command_overrides or {}).get(backend, ""))
    if override:
        if _looks_like_path(override):
            candidate = _valid_path_command(Path(override), backend)
            return str(candidate) if candidate else None
        return _valid_command(shutil.which(override), backend)
    discovered = _discover_command(_default_command_names(backend))
    if discovered:
        return discovered
    if backend == CODEX_BACKEND:
        return _safe_which("codex", backend)
    if backend == CLAUDE_CODE_BACKEND:
        return _safe_which("claude", backend)
    return None


def command_is_runnable(
    backend: str,
    command: str,
    command_overrides: dict[str, str] | None = None,
) -> bool:
    overrides = {**(command_overrides or {}), backend: command}
    return command_for_backend(backend, overrides) is not None


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
        found = _safe_which(name, "")
        if found:
            return found

    for directory in _candidate_command_dirs():
        for name in names:
            candidate = _valid_path_command(directory / name, "")
            if candidate:
                return str(candidate)

    return None


def _safe_which(name: str, backend: str) -> str | None:
    found = shutil.which(name)
    return _valid_command(found, backend) if found else None


def _valid_path_command(path: Path, backend: str) -> Path | None:
    if not path.exists() or path.is_dir() or _is_desktop_app_resource(path):
        return None
    return path if _smoke_test_command(str(path), backend) else None


def _valid_command(command: str | None, backend: str) -> str | None:
    if not command or _is_desktop_app_resource(command):
        return None
    path = Path(command)
    if _looks_like_path(command) and (not path.exists() or path.is_dir()):
        return None
    return command if _smoke_test_command(command, backend) else None


def _smoke_test_command(command: str, backend: str) -> bool:
    args = [command, "--help"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode == 0:
        return True
    output = f"{result.stdout}\n{result.stderr}".lower()
    if backend == CODEX_BACKEND:
        return "codex" in output and "access is denied" not in output
    if backend == CLAUDE_CODE_BACKEND:
        return "claude" in output
    return False


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
