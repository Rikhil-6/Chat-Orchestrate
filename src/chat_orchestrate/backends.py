from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .api_harness import apply_api_harness_response, build_api_harness_prompt
from .models import ProjectSpace
from .models import DelegatedTask


SIMULATED_BACKEND = "simulated"
CODEX_BACKEND = "codex"
CLAUDE_CODE_BACKEND = "claude-code"
GEMINI_CLI_BACKEND = "gemini-cli"
OPEN_SWARM_BACKEND = "openswarm"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "GIT_HTTP_PROXY",
    "GIT_HTTPS_PROXY",
)
BACKEND_RUNTIME_FAILURE_MARKERS = {
    CODEX_BACKEND: (
        "failed to open state db",
        "readonly database",
        "codex_rollout::state_db",
        "failed to initialize state runtime",
        "failed to initialize in-process app-server client",
        "not logged in",
        "authentication failed",
        "auth failed",
        "auth error",
        "unauthorized",
        "api key missing",
        "missing api key",
        "invalid api key",
        "no api key",
        "permission denied",
        "access is denied",
        "did not return final chat text",
        "did not receive a usable response",
    ),
    CLAUDE_CODE_BACKEND: (
        "claude code command",
        "claude command",
        "not logged in",
        "authentication failed",
        "auth failed",
        "auth error",
        "unauthorized",
        "api key missing",
        "missing api key",
        "invalid api key",
        "no api key",
        "anthropic_api_key",
        "claude_api_key",
        "permission denied",
        "access is denied",
        "did not return final chat text",
        "did not receive a usable response",
    ),
    GEMINI_CLI_BACKEND: (
        "gemini command",
        "not logged in",
        "authentication failed",
        "auth failed",
        "auth error",
        "unauthorized",
        "api key missing",
        "missing api key",
        "invalid api key",
        "no api key",
        "gemini_api_key",
        "google_api_key",
        "permission denied",
        "access is denied",
        "did not return final chat text",
        "did not receive a usable response",
    ),
}


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
        if command_for_backend(GEMINI_CLI_BACKEND, command_overrides):
            detected.append(GEMINI_CLI_BACKEND)
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
    api_keys: dict[str, str] | None = None,
    claude_api_model: str = "claude-sonnet-4-5",
    gemini_api_model: str = "gemini-2.0-flash",
    workspaces_root: Path | None = None,
) -> str:
    api_keys = _normalized_api_keys(api_keys, openai_api_key)
    api_models = {
        CODEX_BACKEND: codex_api_model,
        CLAUDE_CODE_BACKEND: claude_api_model,
        GEMINI_CLI_BACKEND: gemini_api_model,
    }
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
        api_key = api_keys.get(task.preferred_backend, "")
        if api_key and backend_supports_api_fallback(task.preferred_backend):
            workspace_path = task_workspace_path(task, workspaces_root)
            api_output = run_backend_api_task(
                task,
                task.preferred_backend,
                api_key,
                api_models[task.preferred_backend],
                workspace_path,
            )
            return (
                f"Backend `{task.preferred_backend}` is not executable on this machine, so this worker "
                f"used the configured {backend_api_name(task.preferred_backend)} API fallback automatically.\n\n"
                f"{api_output}"
            )
        return (
            f"Backend `{task.preferred_backend}` is not executable on this machine.\n\n"
            f"{backend_execution_hint(task.preferred_backend)}"
        )

    workspace_path = task_workspace_path(task, workspaces_root)
    workspace_line = f"Project space path: {workspace_path}\n" if workspace_path else ""
    write_contract = (
        "Workspace write contract:\n"
        "- Treat the project space path as read-write for this assigned task.\n"
        "- If implementation is requested, create or update files there instead of only planning.\n"
        "- If the workspace is empty, scaffold a greenfield app or monorepo structure there.\n"
        "- Do not claim the session is read-only unless an actual write attempt fails; if it fails, report the "
        "exact path, command, and error.\n"
        "- Return the files changed plus the commands needed to preview or verify the work.\n\n"
    )
    prompt = (
        f"You are the `{task.role}` agent for a distributed project run.\n\n"
        f"Task: {task.title}\n"
        f"Project space: {task.project}\n"
        f"{workspace_line}"
        f"{write_contract}"
        f"Goal:\n{task.goal}\n\n"
        "Response contract:\n"
        "- Answer the user's latest request directly first, in normal assistant prose.\n"
        "- Then include exact files changed, preview/verification commands, diagnostics, or blockers as needed.\n"
        "- Do not lead with generic coordination status or proof sections unless that is the answer.\n\n"
        "Return a concrete, useful result for your assigned role. If this is implementation work, describe "
        "the exact files, commands, or code changes you would make or have made."
    )

    final_output_path = codex_final_message_path(workspace_path) if task.preferred_backend == CODEX_BACKEND else None
    args = task_command_args(task.preferred_backend, command, prompt, workspace_path, final_output_path)
    if args is None:
        return f"Backend `{task.preferred_backend}` has no command runner yet."

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        cwd=workspace_path,
        stdin=subprocess.DEVNULL,
        env=backend_process_env(api_keys),
    )
    output = read_text_if_present(final_output_path) or result.stdout.strip() or result.stderr.strip()
    if (
        is_backend_runtime_failure(task.preferred_backend, output)
        and api_keys.get(task.preferred_backend, "")
        and backend_supports_api_fallback(task.preferred_backend)
    ):
        api_output = run_backend_api_task(
            task,
            task.preferred_backend,
            api_keys[task.preferred_backend],
            api_models[task.preferred_backend],
            workspace_path,
        )
        return (
            f"{task.preferred_backend} CLI hit a local runtime/session issue before the model could answer, "
            f"so this worker used the configured {backend_api_name(task.preferred_backend)} API fallback "
            "automatically.\n\n"
            f"{api_output}"
        )
    return output or f"`{task.preferred_backend}` exited with code {result.returncode}."


def task_command_args(
    backend: str,
    command: str,
    prompt: str,
    workspace_path: Path | None = None,
    final_output_path: Path | None = None,
) -> list[str] | None:
    if backend == CODEX_BACKEND:
        args = [command, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write"]
        if workspace_path:
            args.extend(["--cd", str(workspace_path)])
        if final_output_path:
            args.extend(["--output-last-message", str(final_output_path)])
        args.append(prompt)
        return args

    if backend == CLAUDE_CODE_BACKEND:
        args = [command]
        if workspace_path and command_supports_option(command, "--add-dir"):
            args.extend(["--add-dir", str(workspace_path)])
        if command_supports_option(command, "--permission-mode"):
            args.extend(["--permission-mode", "acceptEdits"])
        args.extend(["-p", prompt])
        return args

    if backend == GEMINI_CLI_BACKEND:
        args = [command]
        if workspace_path and command_supports_option(command, "--include-directories"):
            args.extend(["--include-directories", str(workspace_path)])
        if command_supports_option(command, "--approval-mode"):
            args.extend(["--approval-mode", "auto_edit"])
        args.extend(["-p", prompt])
        return args

    return None


def codex_final_message_path(workspace_path: Path | None) -> Path | None:
    if workspace_path is None:
        return None
    harness_dir = workspace_path.resolve() / ".chat-orchestrate"
    harness_dir.mkdir(parents=True, exist_ok=True)
    return harness_dir / f"codex-final-{uuid4().hex}.md"


def read_text_if_present(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _normalized_api_keys(api_keys: dict[str, str] | None = None, openai_api_key: str = "") -> dict[str, str]:
    normalized = {
        backend: str(value or "").strip()
        for backend, value in (api_keys or {}).items()
        if str(value or "").strip()
    }
    if openai_api_key.strip():
        normalized[CODEX_BACKEND] = openai_api_key.strip()
    return normalized


def backend_process_env(api_keys: dict[str, str] | None = None) -> dict[str, str]:
    env = sanitized_agent_environment(os.environ.copy())
    keys = _normalized_api_keys(api_keys)
    if keys.get(CODEX_BACKEND):
        env["OPENAI_API_KEY"] = keys[CODEX_BACKEND]
    if keys.get(CLAUDE_CODE_BACKEND):
        env["ANTHROPIC_API_KEY"] = keys[CLAUDE_CODE_BACKEND]
        env["CLAUDE_API_KEY"] = keys[CLAUDE_CODE_BACKEND]
    if keys.get(GEMINI_CLI_BACKEND):
        env["GEMINI_API_KEY"] = keys[GEMINI_CLI_BACKEND]
        env["GOOGLE_API_KEY"] = keys[GEMINI_CLI_BACKEND]
    return env


def sanitized_agent_environment(env: dict[str, str]) -> dict[str, str]:
    clean = dict(env)
    for key in PROXY_ENV_KEYS:
        if _is_dead_local_proxy(clean.get(key)):
            clean.pop(key, None)
    return clean


def api_httpx_trust_env() -> bool:
    return not any(_is_dead_local_proxy(os.environ.get(key)) for key in PROXY_ENV_KEYS)


def _is_dead_local_proxy(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value if "://" in value else f"http://{value}")
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"} and parsed.port == 9


def is_backend_runtime_failure(backend: str, content: str) -> bool:
    lowered = " ".join(str(content or "").lower().split())
    if not lowered:
        return False
    common = (
        "exited with code",
        "could not start",
        "command is not reachable",
        "not executable",
        "not callable",
    )
    return any(marker in lowered for marker in common) or any(
        marker in lowered for marker in BACKEND_RUNTIME_FAILURE_MARKERS.get(backend, ())
    )


def backend_supports_api_fallback(backend: str) -> bool:
    return backend in {CODEX_BACKEND, CLAUDE_CODE_BACKEND, GEMINI_CLI_BACKEND}


def is_codex_runtime_failure(content: str) -> bool:
    return is_backend_runtime_failure(CODEX_BACKEND, content)


def task_workspace_path(task: DelegatedTask, workspaces_root: Path | None = None) -> Path | None:
    if not workspaces_root:
        return None
    candidate = (workspaces_root / task.project).resolve()
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate if candidate.exists() and candidate.is_dir() else None


def command_supports_option(command: str, option: str) -> bool:
    return option in command_help_text(command)


@lru_cache(maxsize=32)
def command_help_text(command: str) -> str:
    try:
        result = subprocess.run([command, "--help"], capture_output=True, text=True, check=False, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return f"{result.stdout}\n{result.stderr}"


def run_codex_api_task(task: DelegatedTask, api_key: str, model: str) -> str:
    return run_backend_api_task(task, CODEX_BACKEND, api_key, model)


def run_backend_api_task(
    task: DelegatedTask,
    backend: str,
    api_key: str,
    model: str,
    workspace_path: Path | None = None,
) -> str:
    prompt = task_api_prompt(task)
    project = ProjectSpace(task.project, workspace_path) if workspace_path else None
    prompt = build_api_harness_prompt(prompt, project)
    if backend == CODEX_BACKEND:
        output = run_openai_response_api(prompt, api_key, model)
    elif backend == CLAUDE_CODE_BACKEND:
        output = run_claude_messages_api(prompt, api_key, model)
    elif backend == GEMINI_CLI_BACKEND:
        output = run_gemini_generate_content_api(prompt, api_key, model)
    else:
        return f"No API fallback is configured for backend `{backend}`."
    return apply_api_harness_response(project, output).content


def task_api_prompt(task: DelegatedTask) -> str:
    return (
        f"You are the `{task.role}` agent for a distributed project run.\n\n"
        f"Task: {task.title}\n"
        f"Project space: {task.project}\n"
        f"Goal:\n{task.goal}\n\n"
        "Answer the user's latest request directly first, then include concrete files, commands, diagnostics, "
        "or blockers as needed.\n\n"
        "Return a concrete, useful result for your assigned role."
    )


def run_openai_response_api(prompt: str, api_key: str, model: str) -> str:
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": prompt},
            timeout=180,
            trust_env=api_httpx_trust_env(),
        )
    except httpx.HTTPError as exc:
        return f"OpenAI API request failed: {exc}"
    if response.status_code >= 400:
        return f"OpenAI API request failed: HTTP {response.status_code} {response.text}"
    return extract_response_text(response.json())


def run_claude_messages_api(prompt: str, api_key: str, model: str) -> str:
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=180,
            trust_env=api_httpx_trust_env(),
        )
    except httpx.HTTPError as exc:
        return f"Claude API request failed: {exc}"
    if response.status_code >= 400:
        return f"Claude API request failed: HTTP {response.status_code} {response.text}"
    return extract_claude_response_text(response.json())


def run_gemini_generate_content_api(prompt: str, api_key: str, model: str) -> str:
    try:
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
            timeout=180,
            trust_env=api_httpx_trust_env(),
        )
    except httpx.HTTPError as exc:
        return f"Gemini API request failed: {exc}"
    if response.status_code >= 400:
        return f"Gemini API request failed: HTTP {response.status_code} {response.text}"
    return extract_gemini_response_text(response.json())


def backend_api_name(backend: str) -> str:
    if backend == CODEX_BACKEND:
        return "OpenAI"
    if backend == CLAUDE_CODE_BACKEND:
        return "Claude"
    if backend == GEMINI_CLI_BACKEND:
        return "Gemini"
    return backend


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


def extract_claude_response_text(payload: dict) -> str:
    parts = []
    for item in payload.get("content", []):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts).strip() or "No text output returned by the Claude API."


def extract_gemini_response_text(payload: dict) -> str:
    parts = []
    for candidate in payload.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content", {})
        if not isinstance(content, dict):
            continue
        for part in content.get("parts", []):
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts).strip() or "No text output returned by the Gemini API."


def _is_available(backend: str, command_overrides: dict[str, str] | None = None) -> bool:
    return backend == SIMULATED_BACKEND or command_for_backend(backend, command_overrides) is not None


def command_for_backend(backend: str, command_overrides: dict[str, str] | None = None) -> str | None:
    override = _clean_command_value((command_overrides or {}).get(backend, ""))
    if override:
        if _looks_like_path(override):
            candidate = _valid_path_command(Path(override), backend)
            return str(candidate) if candidate else None
        return _valid_command(shutil.which(override), backend)
    discovered = discover_backend_commands(backend)
    if discovered:
        return discovered[0]
    return None


def discover_backend_commands(backend: str, limit: int = 8) -> list[str]:
    discovered = []
    for command in _discover_commands(_default_command_names(backend), backend):
        if command not in discovered:
            discovered.append(command)
        if len(discovered) >= limit:
            break
    return discovered


def _discover_commands(names: list[str], backend: str) -> list[str]:
    commands = []
    for name in names:
        found = _safe_which(name, backend)
        if found:
            commands.append(found)

    for directory in _candidate_command_dirs():
        for candidate in _candidate_files(directory, names):
            found = _valid_path_command(candidate, backend)
            if found:
                commands.append(str(found))

    return commands


def _discover_command(names: list[str]) -> str | None:
    discovered = _discover_commands(names, "")
    if discovered:
        return discovered[0]
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
    if backend == GEMINI_CLI_BACKEND:
        return ["gemini", "gemini.exe", "gemini.cmd"]
    return []


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
    if backend == GEMINI_CLI_BACKEND:
        return "gemini" in output
    return False


def _candidate_command_dirs() -> list[Path]:
    candidates = []
    path_value = os.environ.get("PATH", "")
    candidates.extend(Path(item) for item in path_value.split(os.pathsep) if item)
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
                base / ".bun" / "bin",
                base / ".cargo" / "bin",
                base / "Programs",
                base / "Programs" / "nodejs",
                base / "Microsoft" / "WindowsApps",
                base / "pnpm",
                base / "Yarn" / "bin",
            ]
        )
    if platform.system().lower() == "darwin":
        candidates.extend(
            [
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
                Path("/Applications"),
            ]
        )
    else:
        candidates.extend([Path("/usr/local/bin"), Path("/usr/bin"), Path("/snap/bin")])

    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _candidate_files(directory: Path, names: list[str]) -> list[Path]:
    files = [directory / name for name in names]
    if not directory.exists() or not directory.is_dir():
        return files
    try:
        for child in directory.iterdir():
            if not child.is_dir():
                continue
            for name in names:
                files.append(child / name)
                files.append(child / "bin" / name)
    except OSError:
        pass
    return files


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
