from __future__ import annotations

import atexit
import asyncio
import os
import re
import secrets
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import chainlit as cl
import httpx
from chainlit.input_widget import Select, Switch, TextInput

from chat_orchestrate.a2a import A2A_PROTOCOL_VERSION
from chat_orchestrate.backends import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    GEMINI_CLI_BACKEND,
    OPEN_SWARM_BACKEND,
    SIMULATED_BACKEND,
    backend_availability,
    backend_execution_hint,
    command_for_backend,
    command_is_runnable,
    detect_agent_backends,
    discover_backend_commands,
    launch_backend_app,
    run_task,
)
from chat_orchestrate.capabilities import infer_goal_roles, infer_machine_capabilities
from chat_orchestrate.config import get_settings
from chat_orchestrate.coordination import CoordinationError, CoordinationManager
from chat_orchestrate.models import MachineNode, OrchestrationRun, ProgressUpdate
from chat_orchestrate.orchestrator import Orchestrator
from chat_orchestrate.project_space import ProjectSpaceError, ProjectSpaceManager
from chat_orchestrate.runtime_config import RUNTIME_CONFIG_PATH, clear_runtime_env, save_runtime_env
from chat_orchestrate.swarm_client import build_swarm_client
from chat_orchestrate.ui_state import (
    append_chat,
    clear_chat_history,
    load_chat_history,
    load_credentials,
    load_preferences,
    save_credentials,
    save_preferences,
)

settings = get_settings()
agent_backends = [
    backend
    for backend in detect_agent_backends(settings.configured_backends, settings.command_overrides)
    if backend != SIMULATED_BACKEND
]
agent_roles = []
spaces = ProjectSpaceManager(settings.workspaces_root, settings.workspace_state_path)
coordination = CoordinationManager(
    settings.coordination_state_path,
    settings.machine_id,
    agent_roles,
    agent_backends,
    settings.orchestrator_ttl_seconds,
    settings.cluster_id,
    settings.coordination_token,
    settings.coordination_backend,
    settings.coordination_http_url,
    settings.coordination_http_urls,
)
coordinator_process: subprocess.Popen | None = None
hosted_connection: dict[str, str] = {}
ui_worker_task: asyncio.Task | None = None


def stop_hosted_coordinator() -> None:
    global coordinator_process
    if coordinator_process and coordinator_process.poll() is None:
        coordinator_process.terminate()
    coordinator_process = None
    clear_hosted_pid_files()


def is_hosting_live() -> bool:
    return bool(coordinator_process and coordinator_process.poll() is None)


def stop_ui_worker() -> None:
    global ui_worker_task
    if ui_worker_task and not ui_worker_task.done():
        ui_worker_task.cancel()
    ui_worker_task = None


atexit.register(stop_hosted_coordinator)
atexit.register(stop_ui_worker)


def start_ui_worker() -> None:
    global ui_worker_task
    if ui_worker_task and not ui_worker_task.done():
        return
    ui_worker_task = asyncio.create_task(ui_worker_loop())


async def ui_worker_loop() -> None:
    while True:
        try:
            node = coordination.heartbeat()
            if node.role == "orchestrator":
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            task = coordination.claim_next_task()
        except CoordinationError:
            await asyncio.sleep(settings.worker_poll_seconds)
            continue

        if task is None:
            await asyncio.sleep(settings.worker_poll_seconds)
            continue

        try:
            result = await asyncio.to_thread(
                run_task,
                task,
                not settings.use_local_agent_chat,
                load_command_overrides(),
                load_openai_api_key(),
                settings.codex_api_model,
            )
        except Exception as exc:  # pragma: no cover - defensive UI worker boundary
            try:
                coordination.complete_task(task.task_id, str(exc), status="failed")
            except CoordinationError:
                pass
        else:
            try:
                coordination.complete_task(task.task_id, result)
            except CoordinationError:
                pass


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("last_goal", "")
    cl.user_session.set("last_run", None)
    refresh_advertised_backends(selected_chat_backend(), "")
    try:
        local_node = coordination.heartbeat()
        orchestrator_node = coordination.get_or_elect_orchestrator()
    except CoordinationError as exc:
        if await try_auto_host_coordinator(exc):
            try:
                local_node = coordination.heartbeat()
                orchestrator_node = coordination.get_or_elect_orchestrator()
            except CoordinationError as retry_exc:
                await cl.Message(content=f"Coordination error: {retry_exc}").send()
                return
        else:
            await cl.Message(content=f"Coordination error: {exc}").send()
            return
    existing = spaces.list_spaces()
    if existing:
        cl.user_session.set("project_space", existing[0])
        active = f"`{existing[0].name}`"
    else:
        default_space = spaces.upsert("default", "default")
        cl.user_session.set("project_space", default_space)
        active = "`default`"

    await setup_chat_settings()
    start_ui_worker()
    await restore_chat_history()
    await cl.Message(
        content=(
            f"I'm ready in project space {active}. The dashboard has the machine and connection details "
            "whenever you need them."
        )
    ).send()
    await update_cluster_roster()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    text = message.content.strip()
    if text.startswith("/"):
        await handle_command(text)
        return

    project = cl.user_session.get("project_space")
    if project is None:
        await cl.Message(content="Pick a project space first with `/use <name>`.").send()
        return
    append_chat("user", "You", text)
    cl.user_session.set("last_goal", text)
    selected_backend = normalize_selected_backend(str(cl.user_session.get("agent_backend") or "auto"))
    refresh_advertised_backends(selected_backend, text)
    if not await ensure_selected_backend_ready(selected_backend):
        return

    try:
        coordination.heartbeat()
        coordination.get_or_elect_orchestrator()
    except CoordinationError as exc:
        await cl.Message(content=f"Coordination error: {exc}").send()
        return
    await update_cluster_roster()

    turn_agent_names = infer_goal_roles(text)
    turn_orchestrator = Orchestrator(
        build_swarm_client(
            settings,
            selected_backend,
            load_command_overrides(),
            load_openai_api_key(),
        ),
        turn_agent_names,
        coordination,
        settings.delegated_task_wait_seconds,
    )
    turns = []
    final_run = None
    progress_items: dict[str, str] = {}
    progress_message = cl.Message(content="Starting coordination...")
    await progress_message.send()
    async for event in turn_orchestrator.run(text, project):
        if isinstance(event, ProgressUpdate):
            progress_items[progress_key(event)] = progress_line(event)
            progress_message.content = render_progress(progress_items)
            await progress_message.update()
            continue
        if isinstance(event, OrchestrationRun):
            final_run = event
            continue

        turns.append(event)
        progress_items[f"done:{event.agent}:{event.role}"] = f"- `done` {event.agent} finished its `{event.role}` pass."
        progress_message.content = render_progress(progress_items)
        await progress_message.update()

    if final_run:
        cl.user_session.set("last_run", final_run)
    progress_message.content = "## Coordination Status\n\nReady with the response. Details are tucked into the dashboard."
    await progress_message.update()
    response = conversational_response(final_run, turns)
    append_chat("assistant", "Assistant", response)
    await cl.Message(content=response).send()
    await update_cluster_roster()


@cl.on_settings_update
async def on_settings_update(updated_settings: dict) -> None:
    backend = normalize_selected_backend(str(updated_settings.get("agent_backend", "Select")))
    restore_history = bool(updated_settings.get("restore_history", True))
    visible_api_key = clean_secret_input(updated_settings.get("openai_api_key", ""))
    visible_command = clean_command_input(updated_settings.get("codex_command", ""))
    openai_api_key = visible_api_key if backend == CODEX_BACKEND else clean_secret_input(updated_settings.get("openai_api_key", ""))
    claude_api_key = (
        clean_secret_input(updated_settings.get("claude_api_key", ""))
        or (visible_api_key if backend == CLAUDE_CODE_BACKEND else "")
    )
    gemini_api_key = (
        clean_secret_input(updated_settings.get("gemini_api_key", ""))
        or (visible_api_key if backend == GEMINI_CLI_BACKEND else "")
    )
    codex_command = validated_command_preference(
        CODEX_BACKEND,
        clean_command_input(updated_settings.get("codex_command", "")),
    )
    claude_command = validated_command_preference(
        CLAUDE_CODE_BACKEND,
        clean_command_input(updated_settings.get("claude_command", "")) or (visible_command if backend == CLAUDE_CODE_BACKEND else ""),
    )
    gemini_command = validated_command_preference(
        GEMINI_CLI_BACKEND,
        clean_command_input(updated_settings.get("gemini_command", "")) or (visible_command if backend == GEMINI_CLI_BACKEND else ""),
    )
    cl.user_session.set("agent_backend", backend)
    cl.user_session.set("restore_history", restore_history)
    cl.user_session.set("openai_api_key", openai_api_key)
    cl.user_session.set("claude_api_key", claude_api_key)
    cl.user_session.set("gemini_api_key", gemini_api_key)
    cl.user_session.set("codex_command", codex_command)
    cl.user_session.set("claude_command", claude_command)
    cl.user_session.set("gemini_command", gemini_command)
    refresh_advertised_backends(backend)
    save_agent_credentials(backend, updated_settings)
    save_preferences(
        {
            "agent_backend": backend,
            "restore_history": str(restore_history).lower(),
            "codex_command": codex_command,
            "claude_command": claude_command,
            "gemini_command": gemini_command,
        }
    )
    await setup_chat_settings(backend)
    await update_cluster_roster()


def conversational_response(run: OrchestrationRun | None, turns: list) -> str:
    goal = run.goal if run else str(cl.user_session.get("last_goal") or "")
    if is_lightweight_chat(goal):
        return "Hey, I’m here. Send me what you want the agents to work on, and I’ll keep the coordination details in the dashboard."

    if not turns:
        return "I’m on it. I’ve updated the dashboard with the current coordination state."

    preferred_roles = ["engineer", "backend", "frontend", "coordinator", "researcher", "reviewer", "documenter"]
    chosen = turns[-1]
    for role in preferred_roles:
        match = next((turn for turn in turns if role in turn.role.lower() or role == turn.agent.lower()), None)
        if match:
            chosen = match
            break

    content = brief_agent_chat_content(chosen.content)
    if not content:
        content = "Done. I’ve updated the dashboard with the current coordination state."

    if run and run.delegated_tasks:
        return f"{content}\n\nI’ve tucked the machine routing and task details into the dashboard."
    return content


def progress_line(update: ProgressUpdate) -> str:
    tags = []
    if update.phase:
        tags.append(f"`{update.phase}`")
    if update.assigned_machine:
        tags.append(f"`{update.assigned_machine}`")
    if update.preferred_backend:
        tags.append(f"`{update.preferred_backend}`")
    if update.role:
        tags.append(f"`{update.role}`")
    prefix = f"- {' '.join(tags)} " if tags else "- "
    return prefix + update.message


def progress_key(update: ProgressUpdate) -> str:
    if update.task_id:
        return f"task:{update.task_id}"
    if update.role:
        return f"role:{update.role}"
    return f"phase:{update.phase}"


def render_progress(items: dict[str, str]) -> str:
    lines = list(items.values())[-8:]
    return "## Coordination Status\n\n" + "\n".join(lines)


def is_lightweight_chat(goal: str) -> bool:
    clean = goal.strip().lower()
    return bool(re.fullmatch(r"(hi|hello|hey|yo|sup|test|smoke|hello there|hello .{1,40})", clean))


def brief_agent_chat_content(content: str) -> str:
    clean_content = clean_agent_chat_content(content)
    skipped = {"workstreams", "dependencies", "success", "validation", "routing"}
    for line in clean_content.splitlines():
        clean = line.strip(" -")
        if clean and clean.lower().rstrip(":") not in skipped:
            return clean
    return ""


def clean_agent_chat_content(content: str) -> str:
    lines = []
    skip_next_blank = False
    for line in content.splitlines():
        clean = line.strip()
        if re.fullmatch(r"`[^`]+`\s+local response", clean):
            skip_next_blank = True
            continue
        if re.fullmatch(r"`[^`]+`\s+result from\s+`[^`]+`", clean):
            skip_next_blank = True
            continue
        if skip_next_blank and not clean:
            skip_next_blank = False
            continue
        skip_next_blank = False
        lines.append(line)
    return "\n".join(lines).strip()


async def handle_command(text: str) -> None:
    parts = text.split()
    command = parts[0].lower()

    try:
        if command == "/dashboard":
            await show_dashboard_sidebar()
        elif command == "/help":
            await cl.Message(content=command_help(), actions=machine_actions()).send()
        elif command == "/detect-agents":
            await auto_detect_agents()
        elif command == "/restart-app":
            await restart_app()
        elif command == "/spaces":
            await show_spaces()
        elif command == "/use" and len(parts) == 2:
            project = spaces.get(parts[1])
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Active project space set to `{project.name}`.").send()
        elif command == "/create-space" and len(parts) >= 3:
            name = parts[1]
            path = " ".join(parts[2:])
            project = spaces.upsert(name, path)
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Created and selected `{project.name}` at `{project.path}`.").send()
        elif command == "/worktree" and len(parts) >= 4:
            name, repo_path, branch = parts[1], parts[2], parts[3]
            project = spaces.create_worktree(name, repo_path, branch)
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Created worktree `{project.name}` at `{project.path}`.").send()
        elif command == "/clone" and len(parts) >= 3:
            name, git_url = parts[1], parts[2]
            branch = parts[3] if len(parts) >= 4 else None
            project = spaces.clone_repository(name, git_url, branch)
            cl.user_session.set("project_space", project)
            await cl.Message(content=f"Cloned and selected `{project.name}` at `{project.path}`.").send()
        elif command == "/workspace-modes":
            await show_workspace_modes()
        elif command == "/machines":
            coordination.heartbeat()
            await show_machines()
        elif command == "/claim-orchestrator":
            node = coordination.claim_orchestrator()
            await cl.Message(content=f"`{node.machine_id}` is now the orchestrator machine.").send()
            await show_machines()
        elif command == "/release-orchestrator":
            coordination.release_orchestrator()
            elected = coordination.get_or_elect_orchestrator()
            await cl.Message(content=f"Released claim. Current orchestrator: `{elected.machine_id}`.").send()
            await show_machines()
        elif command == "/tasks":
            await show_tasks()
        elif command == "/backends":
            await show_backends()
        elif command == "/connect":
            await show_connection()
        elif command == "/host-coordinator":
            await host_coordinator()
        elif command == "/connect-coordinator":
            await configure_http_connection()
        elif command == "/connect-http":
            await configure_http_connection()
        elif command == "/connect-file":
            await configure_file_connection()
        elif command == "/end-session":
            await end_session()
        elif command == "/clear-history":
            clear_chat_history()
            await cl.Message(content="Local restored chat history cleared.").send()
        else:
            await cl.Message(content=command_help()).send()
    except ProjectSpaceError as exc:
        await cl.Message(content=f"Project space error: {exc}").send()
    except CoordinationError as exc:
        await cl.Message(content=f"Coordination error: {exc}").send()


async def show_spaces() -> None:
    items = spaces.list_spaces()
    if not items:
        await cl.Message(content="No project spaces registered yet.").send()
        return

    lines = [
        f"- `{space.name}` `{space.mode}`: `{space.path}`"
        f"{f' on `{space.branch}`' if space.branch else ''}"
        f"{f' from `{space.git_remote}`' if space.git_remote else ''}"
        for space in items
    ]
    await cl.Message(content="## Project Spaces\n\n" + "\n".join(lines)).send()


async def setup_chat_settings(selected_backend: str | None = None) -> None:
    preferences = load_preferences()
    credentials = load_credentials()
    restore_history = preferences.get("restore_history", "true").lower() != "false"
    codex_credentials = credentials.get(CODEX_BACKEND, {})
    claude_credentials = credentials.get(CLAUDE_CODE_BACKEND, {})
    gemini_credentials = credentials.get(GEMINI_CLI_BACKEND, {})
    openswarm_credentials = credentials.get(OPEN_SWARM_BACKEND, {})
    openai_api_key = clean_secret_input(codex_credentials.get("openai_api_key", ""))
    claude_api_key = clean_secret_input(claude_credentials.get("claude_api_key", ""))
    gemini_api_key = clean_secret_input(gemini_credentials.get("gemini_api_key", ""))
    codex_command = validated_command_preference(
        CODEX_BACKEND,
        clean_command_input(codex_credentials.get("codex_command", preferences.get("codex_command", settings.codex_command))),
    )
    claude_command = validated_command_preference(
        CLAUDE_CODE_BACKEND,
        clean_command_input(
            claude_credentials.get("claude_command", preferences.get("claude_command", settings.claude_command))
        ),
    )
    gemini_command = validated_command_preference(
        GEMINI_CLI_BACKEND,
        clean_command_input(
            gemini_credentials.get("gemini_command", preferences.get("gemini_command", settings.gemini_command))
        ),
    )
    command_overrides = {
        CODEX_BACKEND: codex_command,
        CLAUDE_CODE_BACKEND: claude_command,
        GEMINI_CLI_BACKEND: gemini_command,
    }
    detected_backends = detect_agent_backends(settings.configured_backends, command_overrides)
    backend_values = [
        "auto",
        CODEX_BACKEND,
        CLAUDE_CODE_BACKEND,
        GEMINI_CLI_BACKEND,
        OPEN_SWARM_BACKEND,
        SIMULATED_BACKEND,
        *detected_backends,
        *agent_backends,
    ]
    unique_backend_values = []
    for backend in backend_values:
        if backend not in unique_backend_values:
            unique_backend_values.append(backend)
    selected_backend = resolve_settings_backend(
        selected_backend or preferences.get("agent_backend", "auto"),
        unique_backend_values,
        command_overrides,
    )
    codex_initial = codex_command or command_for_backend(CODEX_BACKEND, command_overrides) or ""
    claude_initial = claude_command or command_for_backend(CLAUDE_CODE_BACKEND, command_overrides) or ""
    gemini_initial = gemini_command or command_for_backend(GEMINI_CLI_BACKEND, command_overrides) or ""
    cl.user_session.set("agent_backend", selected_backend)
    cl.user_session.set("restore_history", restore_history)
    cl.user_session.set("openai_api_key", openai_api_key)
    cl.user_session.set("claude_api_key", claude_api_key)
    cl.user_session.set("gemini_api_key", gemini_api_key)
    cl.user_session.set("codex_command", codex_initial)
    cl.user_session.set("claude_command", claude_initial)
    cl.user_session.set("gemini_command", gemini_initial)
    refresh_advertised_backends(selected_backend)
    save_preferences(
        {
            "codex_command": codex_command,
            "claude_command": claude_command,
            "gemini_command": gemini_command,
        }
    )
    widgets = [
        Select(
            id="agent_backend",
            label="Local Agent",
            values=unique_backend_values,
            initial_value=selected_backend,
            tooltip="Auto selects the first callable local agent detected on this machine.",
        ),
        Switch(
            id="restore_history",
            label="Restore local chat on refresh",
            initial=restore_history,
            tooltip="Replay recent local chat records when the page reconnects.",
        ),
    ]
    if selected_backend == CODEX_BACKEND:
        widgets.extend(
            [
                TextInput(
                    id="openai_api_key",
                    label="OpenAI API Key",
                    initial=openai_api_key,
                    placeholder="Saved locally; used for Codex API fallback",
                    tooltip="Saved locally in ignored ui_state.json for this machine.",
                ),
                TextInput(
                    id="codex_command",
                    label="Codex Command",
                    initial=codex_initial,
                    placeholder="codex, codex.cmd, or full path",
                    tooltip="Optional local Codex CLI command. API fallback is used if this is blank or unavailable.",
                ),
            ]
        )
    elif selected_backend == CLAUDE_CODE_BACKEND:
        widgets.extend(
            [
                TextInput(
                    id="claude_api_key",
                    label="Claude API Key",
                    initial=claude_api_key,
                    placeholder="Saved locally; optional for Claude SDK/API flows",
                    tooltip="Saved locally in ignored ui_state.json for this machine.",
                ),
                TextInput(
                    id="claude_command",
                    label="Claude Code Command",
                    initial=claude_initial,
                    placeholder="claude, claude.cmd, or full path",
                    tooltip="Command used when Local Agent is claude-code.",
                ),
            ]
        )
    elif selected_backend == GEMINI_CLI_BACKEND:
        widgets.extend(
            [
                TextInput(
                    id="gemini_api_key",
                    label="Gemini API Key",
                    initial=gemini_api_key,
                    placeholder="Saved locally; optional for Gemini API flows",
                    tooltip="Saved locally in ignored ui_state.json for this machine.",
                ),
                TextInput(
                    id="gemini_command",
                    label="Gemini CLI Command",
                    initial=gemini_initial,
                    placeholder="gemini, gemini.cmd, or full path",
                    tooltip="Command used when Local Agent is gemini-cli.",
                ),
            ]
        )
    elif selected_backend == OPEN_SWARM_BACKEND:
        widgets.extend(
            [
                TextInput(
                    id="open_swarm_base_url",
                    label="OpenSwarm URL",
                    initial=openswarm_credentials.get("open_swarm_base_url", settings.open_swarm_base_url),
                    placeholder="http://localhost:8000",
                    tooltip="OpenAI-compatible OpenSwarm endpoint.",
                ),
                TextInput(
                    id="open_swarm_api_key",
                    label="OpenSwarm API Key",
                    initial=openswarm_credentials.get("open_swarm_api_key", settings.open_swarm_api_key),
                    placeholder="Optional",
                    tooltip="Saved locally in ignored ui_state.json for this machine.",
                ),
                TextInput(
                    id="open_swarm_model",
                    label="OpenSwarm Model",
                    initial=openswarm_credentials.get("open_swarm_model", settings.open_swarm_model),
                    placeholder=settings.open_swarm_model,
                    tooltip="Model name used by the OpenSwarm endpoint.",
                ),
            ]
        )

    await cl.ChatSettings(widgets).send()


def refresh_advertised_backends(selected_backend: str = "auto", goal: str = "") -> None:
    global agent_backends, agent_roles
    detected = detect_agent_backends(settings.configured_backends, load_command_overrides())
    advertised = advertised_backends(detected, selected_backend, goal)
    roles = infer_machine_capabilities(
        advertised,
        normalize_selected_backend(selected_backend),
        settings.default_agents,
        goal,
    )
    agent_backends = advertised
    agent_roles = roles
    coordination.agent_backends = advertised
    coordination.agent_roles = roles


def advertised_backends(detected: list[str], selected_backend: str = "auto", goal: str = "") -> list[str]:
    selected = normalize_selected_backend(selected_backend)
    if selected != "auto" and selected != SIMULATED_BACKEND and backend_is_callable(selected):
        return [selected]
    if selected == SIMULATED_BACKEND:
        return [SIMULATED_BACKEND] if goal.strip() else []

    real = [
        backend
        for backend in detected
        if backend != SIMULATED_BACKEND and backend_is_callable(backend)
    ]
    if real:
        return real
    return [SIMULATED_BACKEND] if goal.strip() else []


def normalize_selected_backend(backend: str) -> str:
    clean = str(backend or "").strip()
    return "auto" if clean in {"", "Select", "Auto"} else clean


def resolve_settings_backend(
    requested_backend: str,
    available_values: list[str],
    command_overrides: dict[str, str] | None = None,
) -> str:
    requested = normalize_selected_backend(requested_backend)
    if requested != "auto" and requested in available_values:
        return requested

    for backend in [CODEX_BACKEND, CLAUDE_CODE_BACKEND, GEMINI_CLI_BACKEND, OPEN_SWARM_BACKEND]:
        if backend in available_values and backend_is_callable_with_overrides(backend, command_overrides):
            return backend
    if SIMULATED_BACKEND in available_values:
        return SIMULATED_BACKEND
    return "auto"


def backend_is_callable_with_overrides(backend: str, command_overrides: dict[str, str] | None = None) -> bool:
    if backend in {"auto", "Select", SIMULATED_BACKEND}:
        return True
    if command_for_backend(backend, command_overrides):
        return True
    if backend == CODEX_BACKEND and load_openai_api_key():
        return True
    if backend == OPEN_SWARM_BACKEND:
        return bool(settings.open_swarm_base_url.strip())
    return False


async def ensure_selected_backend_ready(selected_backend: str) -> bool:
    backend = normalize_selected_backend(selected_backend)
    if backend in {"auto", SIMULATED_BACKEND}:
        return True
    if backend_is_callable(backend):
        return True
    await cl.Message(content=backend_setup_needed_cards(backend), actions=backend_setup_actions(backend)).send()
    await show_dashboard_sidebar()
    return False


def backend_is_callable(backend: str) -> bool:
    return backend_is_callable_with_overrides(backend, load_command_overrides())


def backend_setup_needed_cards(backend: str) -> str:
    if backend == CODEX_BACKEND:
        return (
            "## Codex Is Selected, But Not Connected For Headless Runs\n\n"
            "The Codex desktop app can be signed in, but this harness cannot reuse the desktop app session "
            "as a callable backend. For distributed agent execution, this machine needs one of these:\n\n"
            "1. A reachable Codex CLI command on `PATH`, signed in through its normal terminal flow.\n"
            "2. A full path to the Codex CLI command in the sidebar.\n"
            "3. An OpenAI API key saved in the sidebar for the Codex API fallback.\n\n"
            "Use **Auto-detect Agents** after installing/signing in. Use **Restart App** only if you changed "
            "your terminal PATH or installed a command while this app was already running."
        )
    if backend == CLAUDE_CODE_BACKEND:
        return (
            "## Claude Code Is Selected, But Not Connected For Headless Runs\n\n"
            "This harness talks to Claude Code through the local `claude` command. Sign in through Claude "
            "Code's normal terminal flow, make sure `claude` is on `PATH`, or set the full command path in "
            "the sidebar. Use **Auto-detect Agents** after setup, or **Restart App** if PATH changed."
        )
    if backend == GEMINI_CLI_BACKEND:
        return (
            "## Gemini CLI Is Selected, But Not Connected For Headless Runs\n\n"
            "This harness talks to Gemini through the local `gemini` command. Sign in through Gemini CLI's "
            "normal terminal flow, make sure `gemini` is on `PATH`, or set the full command path in the "
            "sidebar. Use **Auto-detect Agents** after setup, or **Restart App** if PATH changed."
        )
    return f"## `{backend}` Is Not Ready\n\n{backend_execution_hint(backend)}"


def backend_setup_actions(backend: str) -> list[cl.Action]:
    actions = [
        cl.Action(
            name="auto_detect_agents",
            label="Auto-detect Agents",
            tooltip="Search common local install paths for Codex, Claude, and Gemini commands.",
            icon="search",
            payload={},
        ),
        cl.Action(
            name="restart_app",
            label="Restart App",
            tooltip="Restart the local app when it is running under scripts/run_local.py.",
            icon="rotate-cw",
            payload={},
        ),
        cl.Action(
            name="show_backends",
            label="Agent Status",
            tooltip="Show local backend availability and setup hints.",
            icon="cpu",
            payload={},
        ),
        cl.Action(
            name="refresh_dashboard",
            label="Dashboard",
            tooltip="Refresh the harness dashboard.",
            icon="layout-dashboard",
            payload={},
        ),
    ]
    if backend == CODEX_BACKEND:
        actions.insert(
            0,
            cl.Action(
                name="launch_codex_app",
                label="Launch Codex App",
                tooltip="Open the installed Codex desktop app for normal sign-in/setup.",
                icon="log-in",
                payload={},
            ),
        )
    return actions


async def auto_detect_agents() -> None:
    detected_commands = {
        CODEX_BACKEND: discover_backend_commands(CODEX_BACKEND),
        CLAUDE_CODE_BACKEND: discover_backend_commands(CLAUDE_CODE_BACKEND),
        GEMINI_CLI_BACKEND: discover_backend_commands(GEMINI_CLI_BACKEND),
    }
    preferences = {}
    credential_updates = []
    if detected_commands[CODEX_BACKEND]:
        command = detected_commands[CODEX_BACKEND][0]
        cl.user_session.set("codex_command", command)
        preferences["codex_command"] = command
        save_credentials(CODEX_BACKEND, {"codex_command": command})
        credential_updates.append(f"- Codex CLI: `{command}`")
    if detected_commands[CLAUDE_CODE_BACKEND]:
        command = detected_commands[CLAUDE_CODE_BACKEND][0]
        cl.user_session.set("claude_command", command)
        preferences["claude_command"] = command
        save_credentials(CLAUDE_CODE_BACKEND, {"claude_command": command})
        credential_updates.append(f"- Claude Code CLI: `{command}`")
    if detected_commands[GEMINI_CLI_BACKEND]:
        command = detected_commands[GEMINI_CLI_BACKEND][0]
        cl.user_session.set("gemini_command", command)
        preferences["gemini_command"] = command
        save_credentials(GEMINI_CLI_BACKEND, {"gemini_command": command})
        credential_updates.append(f"- Gemini CLI: `{command}`")
    if preferences:
        save_preferences(preferences)
    selected_backend = resolve_settings_backend(
        str(cl.user_session.get("agent_backend") or "auto"),
        ["auto", CODEX_BACKEND, CLAUDE_CODE_BACKEND, GEMINI_CLI_BACKEND, OPEN_SWARM_BACKEND, SIMULATED_BACKEND],
        {
            CODEX_BACKEND: str(cl.user_session.get("codex_command") or preferences.get("codex_command", "")),
            CLAUDE_CODE_BACKEND: str(cl.user_session.get("claude_command") or preferences.get("claude_command", "")),
            GEMINI_CLI_BACKEND: str(cl.user_session.get("gemini_command") or preferences.get("gemini_command", "")),
        },
    )
    if selected_backend != "auto":
        cl.user_session.set("agent_backend", selected_backend)
        save_preferences({"agent_backend": selected_backend})
    refresh_advertised_backends(selected_backend, str(cl.user_session.get("last_goal") or ""))
    await setup_chat_settings(selected_backend)
    await show_dashboard_sidebar()
    if credential_updates:
        await cl.Message(
            content="## Agent Commands Detected\n\n" + "\n".join(credential_updates) + "\n\nYou can retry your message now."
        ).send()
    else:
        await cl.Message(
            content=(
                "## No Callable Agent Commands Found\n\n"
                "I checked PATH plus common npm, user-local, WindowsApps, Homebrew, and local bin locations. "
                "If Codex, Claude Code, or Gemini CLI is installed elsewhere, paste the full command path in the sidebar "
                "or restart the app from a terminal where the command works."
            ),
            actions=backend_setup_actions(selected_backend),
        ).send()


async def restart_app() -> None:
    marker = Path(".tmp") / "restart-local"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    await cl.Message(
        content=(
            "Restart requested. If this app was launched with `scripts/run_local.py`, it will come back "
            "automatically. Otherwise, start it again with `./scripts/run_local.sh` or `.\\scripts\\run_local.ps1`."
        )
    ).send()
    await asyncio.sleep(0.5)
    stop_hosted_coordinator()
    stop_ui_worker()
    os._exit(3)


def save_agent_credentials(backend: str, values: dict) -> None:
    if backend == CODEX_BACKEND:
        payload = {
            "openai_api_key": clean_secret_input(values.get("openai_api_key", "")),
            "codex_command": validated_command_preference(
                CODEX_BACKEND,
                clean_command_input(values.get("codex_command", "")),
            ),
        }
        save_credentials(CODEX_BACKEND, payload)
    elif backend == CLAUDE_CODE_BACKEND:
        payload = {
            "claude_api_key": clean_secret_input(values.get("claude_api_key", "")),
            "claude_command": validated_command_preference(
                CLAUDE_CODE_BACKEND,
                clean_command_input(values.get("claude_command", "")),
            ),
        }
        save_credentials(CLAUDE_CODE_BACKEND, payload)
    elif backend == GEMINI_CLI_BACKEND:
        payload = {
            "gemini_api_key": clean_secret_input(values.get("gemini_api_key", "")),
            "gemini_command": validated_command_preference(
                GEMINI_CLI_BACKEND,
                clean_command_input(values.get("gemini_command", "")),
            ),
        }
        save_credentials(GEMINI_CLI_BACKEND, payload)
    elif backend == OPEN_SWARM_BACKEND:
        save_credentials(
            OPEN_SWARM_BACKEND,
            {
                "open_swarm_base_url": clean_command_input(values.get("open_swarm_base_url", "")),
                "open_swarm_api_key": clean_secret_input(values.get("open_swarm_api_key", "")),
                "open_swarm_model": clean_command_input(values.get("open_swarm_model", "")),
            },
        )


def load_command_overrides() -> dict[str, str]:
    try:
        codex_command = cl.user_session.get("codex_command")
        claude_command = cl.user_session.get("claude_command")
        gemini_command = cl.user_session.get("gemini_command")
    except Exception:
        codex_command = None
        claude_command = None
        gemini_command = None
    preferences = load_preferences()
    credentials = load_credentials()
    codex_credentials = credentials.get(CODEX_BACKEND, {})
    claude_credentials = credentials.get(CLAUDE_CODE_BACKEND, {})
    gemini_credentials = credentials.get(GEMINI_CLI_BACKEND, {})
    return {
        CODEX_BACKEND: clean_command_input(
            codex_command
            or codex_credentials.get("codex_command")
            or preferences.get("codex_command", settings.codex_command)
        ),
        CLAUDE_CODE_BACKEND: clean_command_input(
            claude_command
            or claude_credentials.get("claude_command")
            or preferences.get("claude_command", settings.claude_command)
        ),
        GEMINI_CLI_BACKEND: clean_command_input(
            gemini_command
            or gemini_credentials.get("gemini_command")
            or preferences.get("gemini_command", settings.gemini_command)
        ),
    }


def load_openai_api_key() -> str:
    try:
        session_key = cl.user_session.get("openai_api_key")
    except Exception:
        session_key = ""
    credentials = load_credentials()
    codex_credentials = credentials.get(CODEX_BACKEND, {})
    return clean_secret_input(session_key) or clean_secret_input(codex_credentials.get("openai_api_key", "")) or settings.openai_api_key.strip()


def validated_command_preference(backend: str, command: str) -> str:
    if command and command_is_runnable(backend, command, settings.command_overrides):
        return command
    return command_for_backend(backend, settings.command_overrides) or ""


def clean_command_input(value: object) -> str:
    clean = str(value or "").strip().strip('"').strip("'")
    return "" if clean.lower() in {"none", "null", "undefined"} else clean


def clean_secret_input(value: object) -> str:
    clean = str(value or "").strip()
    return "" if clean.lower() in {"none", "null", "undefined"} else clean


async def restore_chat_history() -> None:
    if not cl.user_session.get("restore_history", True):
        return
    history = [
        record
        for record in load_chat_history()
        if record.role == "user" or record.author.lower() == "assistant"
    ]
    if not history:
        return
    for record in history:
        if record.role == "user":
            await cl.Message(author=record.author, content=record.content).send()
        else:
            await cl.Message(author=record.author, content=record.content).send()


async def show_machines() -> None:
    coordination.heartbeat()
    await update_cluster_roster()
    machines = coordination.list_machines()
    orchestrator_node = coordination.get_or_elect_orchestrator()
    content = machine_cards(machines, orchestrator_node.machine_id)
    await cl.Message(
        content=content,
        actions=machine_actions(),
    ).send()


async def show_tasks() -> None:
    tasks = coordination.list_tasks()
    if not tasks:
        await cl.Message(content="No delegated tasks recorded yet.").send()
        return

    lines = [
        f"- `{task.status}` `{task.role}` via `{task.preferred_backend}` "
        f"on `{task.assigned_machine}`: {task.title}"
        for task in tasks
    ]
    await cl.Message(content="## Recent Delegated Tasks\n\n" + "\n".join(lines)).send()


async def show_backends() -> None:
    await cl.Message(content=backend_status_cards(), actions=machine_actions()).send()


async def show_connection() -> None:
    await update_cluster_roster()
    await cl.Message(content=connection_cards(), actions=machine_actions()).send()


async def configure_http_connection() -> None:
    await cl.Message(
        content=(
            "Connect this machine to a hosted coordinator. Paste the connection pack from the host, "
            "or paste just the coordinator URL."
        )
    ).send()
    connection_text = await ask_text(
        "Coordinator connection pack or URL.",
        settings.coordination_http_urls or settings.coordination_http_url,
    )
    if connection_text is None:
        return
    parsed = parse_connection_input(connection_text)
    coordinator_urls = parsed["urls"]
    if not coordinator_urls:
        await cl.Message(content="Add at least one coordinator URL.").send()
        return

    status = cl.Message(content="Checking coordinator reachability...")
    await status.send()
    health = await fetch_coordinator_health(coordinator_urls, status_message=status)
    if health is None:
        await cl.Message(content=coordinator_reachability_help(coordinator_urls)).send()
        return

    live_url, health_cluster_id = health
    cluster_id = parsed["cluster_id"] or health_cluster_id or settings.cluster_id
    token = parsed["token"] or settings.coordination_token
    if not token:
        token = await ask_text("Shared token shown by the host.")
    if token is None or not token:
        await cl.Message(content="A shared token is required before joining a coordinator.").send()
        return

    validation_error = await validate_coordinator_token(live_url, cluster_id, token)
    if validation_error:
        await cl.Message(
            content=(
                f"Coordinator token check failed: {validation_error}\n\n"
                "The URL is reachable, but that coordinator is not using the token you pasted. "
                "On the host machine, click **End Session**, then **Host** again and copy the newest "
                "connection pack. If the host had an old coordinator terminal open, close it too."
            )
        ).send()
        return

    save_runtime_env(
        {
            "COORDINATION_BACKEND": "http",
            "COORDINATION_HTTP_URL": live_url,
            "COORDINATION_HTTP_URLS": ",".join(prioritize_url(coordinator_urls, live_url)),
            "CLUSTER_ID": cluster_id,
            "COORDINATION_TOKEN": token,
            "COORDINATOR_AUTO_HOST": str(settings.coordinator_auto_host).lower(),
            "MACHINE_ID": settings.machine_id or socket.gethostname().lower(),
            "AGENT_BACKENDS": ",".join(agent_backends),
        }
    )
    apply_http_connection(prioritize_url(coordinator_urls, live_url), cluster_id, token)
    try:
        node = coordination.heartbeat()
        orchestrator_node = coordination.get_or_elect_orchestrator()
    except CoordinationError as exc:
        await cl.Message(content=f"Saved connection, but live heartbeat failed: {exc}").send()
        return

    await cl.Message(
        content=(
            "## Connected To Coordinator\n\n"
            f"Coordinator URL: `{coordination.http_url}`  \n"
            f"Cluster: `{cluster_id}`  \n"
            f"Machine: `{node.machine_id}`  \n"
            f"Orchestrator: `{orchestrator_node.machine_id}`\n\n"
            f"Saved to `{RUNTIME_CONFIG_PATH}`. This UI session is using the coordinator now."
        )
    ).send()
    await show_machines()


async def host_coordinator() -> None:
    global coordinator_process, hosted_connection
    if settings.coordination_backend.lower().strip() == "http" and not is_hosting_live():
        await cl.Message(
            content=(
                "This chat is already connected to a coordinator, so it cannot host another coordinator in "
                "the same session. Use **End Session** first, or start a separate chat/session if this "
                "machine should host a different cluster."
            )
        ).send()
        await show_dashboard_sidebar()
        return
    if coordinator_process and coordinator_process.poll() is None:
        await cl.Message(
            content=(
                "Existing hosted coordinator found. Rotating the connection token and restarting the "
                "coordinator so the new token replaces the old session."
            )
        ).send()
        stop_hosted_coordinator()
        hosted_connection = {}

    await cl.Message(content="Starting a coordinator on this machine...").send()
    default_cluster = settings.cluster_id if settings.cluster_id != "local" else "friends-project"
    cluster_id = default_cluster
    token = secrets.token_urlsafe(24)
    port = find_available_port(settings.coordinator_port)
    if port != settings.coordinator_port:
        await cl.Message(
            content=(
                f"Port `{settings.coordinator_port}` is already in use on this machine. "
                f"Hosting on available port `{port}` instead."
            )
        ).send()
    machine_id = settings.machine_id or socket.gethostname().lower()
    backends = ",".join(agent_backends)

    status = cl.Message(content=f"Starting coordinator on port `{port}`...")
    await status.send()
    if not await start_hosted_coordinator(cluster_id, token, port, status_message=status):
        await cl.Message(
            content=(
                "Coordinator did not become reachable locally. The port may be blocked, unavailable, "
                "or the coordinator process may have exited."
            )
        ).send()
        coordinator_process = None
        return

    self_url = default_coordinator_url(port)
    candidate_urls = local_coordinator_urls(port)
    save_runtime_env(
        {
            "COORDINATION_BACKEND": "http",
            "COORDINATION_HTTP_URL": self_url,
            "COORDINATION_HTTP_URLS": ",".join(candidate_urls),
            "CLUSTER_ID": cluster_id,
            "COORDINATION_TOKEN": token,
            "COORDINATOR_AUTO_HOST": "true",
            "COORDINATOR_HOST": "0.0.0.0",
            "COORDINATOR_PORT": str(port),
            "MACHINE_ID": machine_id,
            "AGENT_BACKENDS": backends,
        }
    )
    hosted_connection = {"cluster_id": cluster_id, "token": token, "port": str(port)}
    apply_http_connection(candidate_urls, cluster_id, token, auto_host=True)
    try:
        coordination.heartbeat()
        coordination.get_or_elect_orchestrator()
    except CoordinationError:
        pass
    await cl.Message(content=hosted_coordinator_message(cluster_id, token, port)).send()
    await show_dashboard_sidebar()


async def configure_file_connection() -> None:
    cluster_id = await ask_text("Cluster ID.", settings.cluster_id)
    if cluster_id is None:
        return
    token = await ask_text("Shared coordination token.", settings.coordination_token)
    if token is None:
        return
    machine_id = await ask_text("This machine ID.", settings.machine_id or socket.gethostname().lower())
    if machine_id is None:
        return
    backends = await ask_text("Agent backends for this machine.", ",".join(agent_backends))
    if backends is None:
        return

    save_runtime_env(
        {
            "COORDINATION_BACKEND": "file",
            "COORDINATION_HTTP_URL": "",
            "CLUSTER_ID": cluster_id,
            "COORDINATION_TOKEN": token,
            "MACHINE_ID": machine_id,
            "AGENT_BACKENDS": backends,
        }
    )
    await cl.Message(content=restart_required_message("Local file coordination settings saved.")).send()


async def end_session() -> None:
    global hosted_connection
    stop_hosted_coordinator()
    clear_hosted_state_files()
    hosted_connection = {}
    clear_runtime_env()
    apply_local_file_connection()
    await cl.Message(
        content=(
            "## Session Ended\n\n"
            "Stopped any coordinator hosted by this UI and wiped local runtime connection config.\n\n"
            f"Removed: `{RUNTIME_CONFIG_PATH}`\n\n"
            "This UI is back on local file coordination. Other machines should also use **End Session** "
            "if they were connected with the old token."
        )
    ).send()
    await update_cluster_roster()


async def end_host() -> None:
    global hosted_connection
    was_hosting = is_hosting_live()
    stop_hosted_coordinator()
    clear_hosted_state_files()
    hosted_connection = {}
    clear_runtime_env()
    apply_local_file_connection()
    await cl.Message(
        content=(
            "## Host Ended\n\n"
            f"{'Stopped the hosted coordinator' if was_hosting else 'No live hosted coordinator process was attached to this UI'}, "
            "removed hosted machine state for this cluster, and wiped the saved coordinator URL/token for this session.\n\n"
            "Connected machines should use **End Session** or reconnect with a fresh connection pack."
        )
    ).send()
    await update_cluster_roster()


async def ask_text(prompt: str, default: str = "") -> str | None:
    suffix = f"\n\nCurrent/default: `{default}`" if default else ""
    response = await cl.AskUserMessage(content=prompt + suffix, timeout=180).send()
    if response is None:
        await cl.Message(content="Configuration cancelled.").send()
        return None
    value = str(response.get("output", "")).strip()
    if value:
        return value
    return default.strip()


async def show_workspace_modes() -> None:
    await update_cluster_roster()
    await cl.Message(
        content=(
            "## Workspace Modes\n\n"
            "> ### Worktree Mode\n"
            "> Use when agents are working on the same repo/project with isolated branches.  \n"
            "> Command: `/worktree <name> <repo-path> <branch>`\n\n"
            "> ### Clone Mode\n"
            "> Use when agents should work on separate copies or competing versions of the same repo.  \n"
            "> Command: `/clone <name> <git-url> [branch]`\n\n"
            "> ### Local Folder Mode\n"
            "> Use when the project already exists as a local folder.  \n"
            "> Command: `/create-space <name> <path>`"
        )
    ).send()


@cl.action_callback("refresh_machines")
async def refresh_machines(_: cl.Action) -> None:
    await show_machines()


@cl.action_callback("refresh_dashboard")
async def refresh_dashboard(_: cl.Action) -> None:
    await show_dashboard_sidebar()


@cl.action_callback("claim_orchestrator")
async def claim_orchestrator(_: cl.Action) -> None:
    node = coordination.claim_orchestrator()
    await cl.Message(content=f"`{node.machine_id}` claimed orchestrator status.").send()
    await show_machines()


@cl.action_callback("release_orchestrator")
async def release_orchestrator(_: cl.Action) -> None:
    coordination.release_orchestrator()
    elected = coordination.get_or_elect_orchestrator()
    await cl.Message(content=f"Released orchestrator claim. Current orchestrator: `{elected.machine_id}`.").send()
    await show_machines()


@cl.action_callback("show_tasks")
async def show_tasks_action(_: cl.Action) -> None:
    await show_tasks()


@cl.action_callback("show_backends")
async def show_backends_action(_: cl.Action) -> None:
    await show_backends()


@cl.action_callback("auto_detect_agents")
async def auto_detect_agents_action(_: cl.Action) -> None:
    await auto_detect_agents()


@cl.action_callback("restart_app")
async def restart_app_action(_: cl.Action) -> None:
    await restart_app()


@cl.action_callback("launch_codex_app")
async def launch_codex_app_action(_: cl.Action) -> None:
    if launch_backend_app(CODEX_BACKEND):
        await cl.Message(
            content=(
                "Launched the Codex desktop app. Sign in or complete setup there, then return here and retry. "
                "For headless agent execution, Chainlit still needs a callable Codex CLI/API."
            )
        ).send()
    else:
        await cl.Message(content="Could not find a launchable Codex desktop app on this machine.").send()


@cl.action_callback("show_connection")
async def show_connection_action(_: cl.Action) -> None:
    await show_connection()


@cl.action_callback("host_coordinator")
async def host_coordinator_action(_: cl.Action) -> None:
    await host_coordinator()


@cl.action_callback("configure_http")
async def configure_http_action(_: cl.Action) -> None:
    await configure_http_connection()


@cl.action_callback("configure_file")
async def configure_file_action(_: cl.Action) -> None:
    await configure_file_connection()


@cl.action_callback("end_session")
async def end_session_action(_: cl.Action) -> None:
    await end_session()


@cl.action_callback("end_host")
async def end_host_action(_: cl.Action) -> None:
    await end_host()


@cl.action_callback("clear_history")
async def clear_history_action(_: cl.Action) -> None:
    clear_chat_history()
    await cl.Message(content="Local restored chat history cleared.").send()


def machine_actions() -> list[cl.Action]:
    return [
        cl.Action(
            name="refresh_dashboard",
            label="Dashboard",
            tooltip="Open or refresh the harness dashboard sidebar.",
            icon="layout-dashboard",
            payload={},
        ),
        cl.Action(
            name="refresh_machines",
            label="Refresh Machines",
            tooltip="Refresh machine heartbeat and status.",
            icon="refresh-cw",
            payload={},
        ),
        cl.Action(
            name="claim_orchestrator",
            label="Claim Orchestrator",
            tooltip="Make this machine the orchestrator.",
            icon="crown",
            payload={},
        ),
        cl.Action(
            name="release_orchestrator",
            label="Release Orchestrator",
            tooltip="Release this machine's orchestrator claim.",
            icon="unlink",
            payload={},
        ),
        cl.Action(
            name="show_tasks",
            label="Recent Tasks",
            tooltip="Show delegated tasks recorded by the orchestrator.",
            icon="list-checks",
            payload={},
        ),
        cl.Action(
            name="show_backends",
            label="Backends",
            tooltip="Show local agent backend availability.",
            icon="cpu",
            payload={},
        ),
        cl.Action(
            name="auto_detect_agents",
            label="Auto-detect Agents",
            tooltip="Search common local install paths for Codex, Claude, and Gemini commands.",
            icon="search",
            payload={},
        ),
        cl.Action(
            name="restart_app",
            label="Restart App",
            tooltip="Restart the local app when launched through scripts/run_local.py.",
            icon="rotate-cw",
            payload={},
        ),
        cl.Action(
            name="launch_codex_app",
            label="Launch Codex App",
            tooltip="Open the installed Codex desktop app for login/setup.",
            icon="log-in",
            payload={},
        ),
        cl.Action(
            name="show_connection",
            label="Connection",
            tooltip="Show how this machine can join or host a shared cluster.",
            icon="network",
            payload={},
        ),
        cl.Action(
            name="host_coordinator",
            label="Host Coordinator",
            tooltip="Start the shared HTTP coordinator from this machine.",
            icon="radio-tower",
            payload={},
        ),
        cl.Action(
            name="configure_http",
            label="Connect to Coordinator",
            tooltip="Join a hosted coordinator through the UI.",
            icon="plug",
            payload={},
        ),
        cl.Action(
            name="configure_file",
            label="Use Local File",
            tooltip="Switch this machine back to local file coordination.",
            icon="file-json",
            payload={},
        ),
        cl.Action(
            name="end_session",
            label="End Session",
            tooltip="Stop hosted coordinator and wipe saved coordinator URL/token.",
            icon="log-out",
            payload={},
        ),
        cl.Action(
            name="clear_history",
            label="Clear History",
            tooltip="Clear the locally restored chat records for this machine.",
            icon="trash-2",
            payload={},
        ),
    ]


def machine_cards(machines, orchestrator_id: str) -> str:
    online_count = sum(1 for machine in machines if _machine_status(machine) == "online")
    stale_count = len(machines) - online_count
    cards = "\n".join(_format_machine_card(machine, orchestrator_id) for machine in machines)
    return (
        "## Machine Status\n\n"
        f"> **Orchestrator** `{orchestrator_id}`  \n"
        f"> **Online** `{online_count}`  \n"
        f"> **Stale** `{stale_count}`\n\n"
        f"{cards}"
    )


async def update_cluster_roster() -> None:
    try:
        coordination.heartbeat()
        machines = coordination.list_machines()
        orchestrator_node = coordination.get_or_elect_orchestrator()
    except CoordinationError:
        return

    content = cluster_roster_cards(machines, orchestrator_node.machine_id)
    existing = cl.user_session.get("cluster_roster_message")
    if existing is not None:
        try:
            existing.content = content
            await existing.update()
        except Exception:
            pass
    await show_dashboard_sidebar(machines, orchestrator_node.machine_id)


async def show_dashboard_sidebar(
    machines: list[MachineNode] | None = None,
    orchestrator_id: str | None = None,
) -> None:
    try:
        coordination.heartbeat()
        machines = machines or coordination.list_machines()
        orchestrator_id = orchestrator_id or coordination.get_or_elect_orchestrator().machine_id
    except CoordinationError:
        return
    await cl.ElementSidebar.set_title("Harness Dashboard")
    await cl.ElementSidebar.set_elements(
        [
            cl.CustomElement(
                name="HarnessDashboard",
                display="side",
                props=dashboard_props(machines, orchestrator_id),
            )
        ]
    )


def dashboard_props(machines: list[MachineNode], orchestrator_id: str) -> dict:
    project = cl.user_session.get("project_space")
    last_goal = str(cl.user_session.get("last_goal") or "")
    selected_backend = selected_chat_backend()
    selected_ready = backend_is_callable(selected_backend)
    local_capabilities = agent_roles if not selected_ready else infer_machine_capabilities(
        agent_backends,
        selected_backend,
        settings.default_agents,
        last_goal,
    )
    online_count = sum(1 for machine in machines if _machine_status(machine) == "online")
    hosting_live = is_hosting_live()
    connected_to_http = settings.coordination_backend.lower().strip() == "http"
    can_host = not connected_to_http or hosting_live
    host_port = hosted_connection.get("port") or str(settings.coordinator_port if hosting_live else "")
    coordinator_urls = local_coordinator_urls(int(host_port)) if hosting_live and host_port.isdigit() else []
    a2a_base_url = ""
    if hosting_live and coordinator_urls:
        a2a_base_url = coordinator_urls[0]
    elif connected_to_http:
        a2a_base_url = coordination.http_url or settings.coordination_http_url
    return {
        "overview": {
            "cluster_id": settings.cluster_id,
            "coordination_backend": settings.coordination_backend,
            "orchestrator_id": orchestrator_id,
            "machine_id": settings.machine_id or socket.gethostname().lower(),
            "local_backend": local_chat_backend_label(),
            "online_count": online_count,
            "stale_count": len(machines) - online_count,
            "hosting_live": hosting_live,
            "connected_to_http": connected_to_http,
            "can_host": can_host,
            "host_port": host_port,
            "coordinator_url": coordination.http_url or settings.coordination_http_url,
            "host_urls": coordinator_urls,
            "token_set": bool(settings.coordination_token),
            "a2a_enabled": hosting_live or connected_to_http,
            "a2a_version": A2A_PROTOCOL_VERSION,
            "a2a_agent_card_url": f"{a2a_base_url.rstrip('/')}/.well-known/agent-card.json" if a2a_base_url else "",
            "a2a_rpc_url": f"{a2a_base_url.rstrip('/')}/a2a/rpc" if a2a_base_url else "",
        },
        "workspace": {
            "name": project.name if project else "default",
            "path": str(project.path) if project else str(settings.workspaces_root / "default"),
            "mode": project.mode if project else "local",
            "branch": project.branch if project else "",
            "remote": project.git_remote if project else "",
        },
        "policy": {
            "selected_backend": selected_backend,
            "selected_ready": selected_ready,
            "capabilities": local_capabilities,
            "goal_roles": infer_goal_roles(last_goal) if last_goal else [],
            "last_goal": last_goal,
        },
        "repo": {
            "has_goal": bool(last_goal),
            "worker_outputs": "waiting for task",
            "canonical": "waiting for workspace decision",
            "merge": "waiting for worker outputs",
        },
        "run": dashboard_run_props(),
        "machines": [machine_dashboard_props(machine) for machine in machines],
    }


def machine_dashboard_props(machine: MachineNode) -> dict:
    age = datetime.now(UTC) - machine.last_seen
    return {
        "machine_id": machine.machine_id,
        "hostname": machine.hostname,
        "role": machine.role,
        "status": machine.status,
        "status_label": _machine_status(machine),
        "seen_seconds": max(0, int(age.total_seconds())),
        "capabilities": machine.capabilities,
        "agent_backends": machine.agent_backends,
    }


def dashboard_run_props() -> dict:
    run = cl.user_session.get("last_run")
    if not run:
        return {
            "run_id": "",
            "goal": "",
            "orchestrator_machine": "",
            "turns": [],
            "tasks": [],
        }
    tasks = recent_dashboard_tasks(getattr(run, "run_id", ""))
    return {
        "run_id": getattr(run, "run_id", ""),
        "goal": getattr(run, "goal", ""),
        "orchestrator_machine": getattr(run, "orchestrator_machine", ""),
        "turns": [
            {
                "agent": turn.agent,
                "role": turn.role,
                "machine": turn.assigned_machine or "",
                "backend": turn.preferred_backend or "",
                "summary": first_content_line(turn.content),
            }
            for turn in getattr(run, "turns", [])[-6:]
        ],
        "tasks": tasks,
    }


def recent_dashboard_tasks(run_id: str) -> list[dict]:
    if not run_id:
        return []
    try:
        tasks = coordination.list_tasks()
    except CoordinationError:
        return []
    if run_id:
        tasks = [task for task in tasks if task.run_id == run_id]
    return [
        {
            "role": task.role,
            "machine": task.assigned_machine,
            "backend": task.preferred_backend,
            "status": task.status,
            "title": task.title,
        }
        for task in tasks[-8:]
    ]


def first_content_line(content: str) -> str:
    clean_content = clean_agent_chat_content(content)
    for line in clean_content.splitlines():
        clean = line.strip(" -")
        if clean:
            return clean[:140]
    return "No output captured."


def cluster_roster_cards(machines, orchestrator_id: str) -> str:
    online = [machine for machine in machines if _machine_status(machine) == "online"]
    stale = [machine for machine in machines if _machine_status(machine) != "online"]
    rows = "\n".join(_format_roster_row(machine, orchestrator_id) for machine in machines)
    return (
        "## Cluster Roster\n\n"
        f"> **Coordinator** `{orchestrator_id}`  \n"
        f"> **Online** `{len(online)}`  **Stale** `{len(stale)}`  \n"
        f"> **Local chat backend** `{local_chat_backend_label()}`\n\n"
        f"{rows}"
    )


def _format_roster_row(machine, orchestrator_id: str) -> str:
    status = _machine_status(machine)
    role = "coordinator" if machine.machine_id == orchestrator_id else machine.role
    backends = " ".join(f"`{backend}`" for backend in machine.agent_backends)
    return f"> `{status}` **{machine.machine_id}** `{role}` - agents {backends}\n"


def cluster_roster_actions() -> list[cl.Action]:
    return [
        cl.Action(
            name="refresh_dashboard",
            label="Dashboard",
            tooltip="Open or refresh the harness dashboard sidebar.",
            icon="layout-dashboard",
            payload={},
        ),
        cl.Action(
            name="refresh_machines",
            label="Refresh",
            tooltip="Refresh connected machines.",
            icon="refresh-cw",
            payload={},
        ),
        cl.Action(
            name="host_coordinator",
            label="Host Coordinator",
            tooltip="Start the shared HTTP coordinator from this machine.",
            icon="radio-tower",
            payload={},
        ),
        cl.Action(
            name="configure_http",
            label="Connect",
            tooltip="Connect this machine to a hosted coordinator.",
            icon="plug",
            payload={},
        ),
        cl.Action(
            name="show_backends",
            label="Agents",
            tooltip="Show local agent backend availability.",
            icon="cpu",
            payload={},
        ),
        cl.Action(
            name="auto_detect_agents",
            label="Detect",
            tooltip="Search common local install paths for Codex, Claude, and Gemini commands.",
            icon="search",
            payload={},
        ),
        cl.Action(
            name="restart_app",
            label="Restart",
            tooltip="Restart the local app when launched through scripts/run_local.py.",
            icon="rotate-cw",
            payload={},
        ),
        cl.Action(
            name="launch_codex_app",
            label="Launch Codex",
            tooltip="Open the installed Codex desktop app for login/setup.",
            icon="log-in",
            payload={},
        ),
        cl.Action(
            name="clear_history",
            label="Clear History",
            tooltip="Clear locally restored chat records.",
            icon="trash-2",
            payload={},
        ),
    ]


def local_chat_backend_label() -> str:
    selected_backend = selected_chat_backend()
    if selected_backend != "auto":
        return selected_backend
    available = [item.name for item in backend_availability(agent_backends, load_command_overrides()) if item.available]
    for backend in available:
        if backend != "simulated":
            return backend
    return "simulated"


def selected_chat_backend() -> str:
    try:
        selected = cl.user_session.get("agent_backend")
    except Exception:
        selected = None
    if selected:
        return normalize_selected_backend(str(selected))
    return normalize_selected_backend(load_preferences().get("agent_backend", "auto"))


def _format_machine_card(machine, orchestrator_id: str) -> str:
    age = datetime.now(UTC) - machine.last_seen
    seen_seconds = max(0, int(age.total_seconds()))
    status = _machine_status(machine)
    lead = "orchestrator" if machine.machine_id == orchestrator_id else machine.role
    capabilities = " ".join(f"`{capability}`" for capability in machine.capabilities)
    backends = " ".join(f"`{backend}`" for backend in machine.agent_backends)
    return (
        f"> ### {machine.machine_id}\n"
        f"> `{status}` `{lead}` `seen {seen_seconds}s ago`  \n"
        f"> Host: `{machine.hostname}`  \n"
        f"> Backends: {backends}  \n"
        f"> Capabilities: {capabilities}\n"
    )


def _machine_status(machine) -> str:
    age = datetime.now(UTC) - machine.last_seen
    return "online" if age.total_seconds() <= settings.orchestrator_ttl_seconds else "stale"


def command_help() -> str:
    return (
        "## Commands\n\n"
        "- `/dashboard`\n"
        "- `/help`\n"
        "- `/detect-agents`\n"
        "- `/restart-app`\n"
        "- `/spaces`\n"
        "- `/use <name>`\n"
        "- `/create-space <name> <path>`\n"
        "- `/worktree <name> <repo-path> <branch>`\n"
        "- `/clone <name> <git-url> [branch]`\n"
        "- `/workspace-modes`\n"
        "- `/machines`\n"
        "- `/claim-orchestrator`\n"
        "- `/release-orchestrator`\n"
        "- `/tasks`\n"
        "- `/backends`\n"
        "- `/connect`\n"
        "- `/host-coordinator`\n"
        "- `/connect-coordinator`\n"
        "- `/connect-http`\n"
        "- `/connect-file`\n"
        "- `/end-session`\n"
        "- `/clear-history`"
    )


def backend_status_cards() -> str:
    visible_backends = []
    for backend in [CODEX_BACKEND, CLAUDE_CODE_BACKEND, GEMINI_CLI_BACKEND, OPEN_SWARM_BACKEND, SIMULATED_BACKEND, *agent_backends]:
        if backend not in visible_backends:
            visible_backends.append(backend)
    items = backend_availability(visible_backends, load_command_overrides())
    lines = []
    for item in items:
        state = backend_readiness_label(item.name, item.available, bool(item.installed_app))
        command = f" `{item.command}`" if item.command else ""
        app = f"\n> App: `{item.installed_app}`" if item.installed_app else ""
        hint = "" if item.available else f"\n> {backend_execution_hint(item.name)}"
        lines.append(f"> ### {item.name}\n> `{state}`{command}{app}{hint}\n")
    return "## Agent Backends\n\n" + "\n".join(lines)


def backend_readiness_label(backend: str, command_available: bool, installed_app: bool) -> str:
    if command_available:
        return "headless-ready"
    if backend == CODEX_BACKEND and load_openai_api_key():
        return "api-fallback-ready"
    if installed_app:
        return "app-installed-login-only"
    return "not-ready"


def connection_cards() -> str:
    backend = settings.coordination_backend.lower().strip() or "file"
    token_state = "set" if settings.coordination_token else "not set"
    if backend == "http":
        target = settings.coordination_http_url or "not configured"
        return (
            "## Cluster Connection\n\n"
            "> ### Current Mode\n"
            f"> Backend: `{backend}`  \n"
            f"> Coordinator URL: `{target}`  \n"
            f"> Fallback URLs: `{settings.coordination_http_urls or 'not set'}`  \n"
            f"> Auto-host fallback: `{settings.coordinator_auto_host}`  \n"
            f"> Cluster: `{settings.cluster_id}`  \n"
            f"> Token: `{token_state}`  \n"
            f"> Saved UI config: `{RUNTIME_CONFIG_PATH}`\n\n"
            "> ### What This Means\n"
            "> This machine is using a shared HTTP coordinator. Any other machine with the same "
            "`COORDINATION_HTTP_URL`, `CLUSTER_ID`, and `COORDINATION_TOKEN` should appear in "
            "`/machines` after it starts.\n"
        )

    lan_urls = " ".join(f"`http://{address}:8765`" for address in local_lan_addresses())
    if not lan_urls:
        lan_urls = "`http://<this-machine-lan-ip>:8765`"
    return (
        "## Cluster Connection\n\n"
        "> ### Current Mode\n"
        f"> Backend: `{backend}`  \n"
        f"> State file: `{settings.coordination_state_path}`  \n"
        f"> Cluster: `{settings.cluster_id}`  \n"
        f"> Token: `{token_state}`  \n"
        f"> Saved UI config: `{RUNTIME_CONFIG_PATH}`\n\n"
        "> ### Why Two Laptops May Both Show Online 1\n"
        "> If both are using local file mode, each laptop is writing its own state file. Same Wi-Fi "
        "does not share that file automatically.\n\n"
        "> ### Same Wi-Fi Option\n"
        "> Click **Host Coordinator** on one machine. It will generate a token and show a "
        "connection pack for the others. Manual equivalent:\n\n"
        "```powershell\n"
        '.\\scripts\\run_coordinator.ps1 -HostName 0.0.0.0 -Port 8765 '
        '-ClusterId friends-project -Token "share-this-out-of-band"\n'
        "```\n\n"
        f"> Other machines can try: {lan_urls}\n\n"
        "> On every other UI or worker, click **Connect to Coordinator** and paste the URL.\n\n"
        "> The manual env shape is:\n\n"
        "```env\n"
        "COORDINATION_BACKEND=http\n"
        "COORDINATION_HTTP_URL=http://<coordinator-lan-ip>:8765\n"
        "CLUSTER_ID=friends-project\n"
        "COORDINATION_TOKEN=share-this-out-of-band\n"
        "```\n\n"
        "> ### Different Networks Option\n"
        "> Put the coordinator behind a private tunnel, VPN, or VPS URL, then use that HTTPS URL as "
        "`COORDINATION_HTTP_URL` on every machine.\n"
    )


def local_lan_addresses() -> list[str]:
    addresses = set()
    hostname = socket.gethostname()
    try:
        for item in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            address = item[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except socket.gaierror:
        pass
    return sorted(addresses)


def default_coordinator_url(port: int) -> str:
    return local_coordinator_urls(port)[0]


def local_coordinator_urls(port: int) -> list[str]:
    addresses = local_lan_addresses()
    if not addresses:
        addresses = ["127.0.0.1"]
    return [f"http://{address}:{port}" for address in addresses]


def hosted_state_path(cluster_id: str, port: int) -> Path:
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in cluster_id)
    return Path(".tmp") / f"hosted-coordinator-{safe or 'cluster'}-{port}.json"


def hosted_pid_path(cluster_id: str, port: int) -> Path:
    return hosted_state_path(cluster_id, port).with_suffix(".pid")


def clear_hosted_pid_files() -> None:
    for path in Path(".tmp").glob("hosted-coordinator-*.pid"):
        try:
            path.unlink()
        except OSError:
            pass


def clear_hosted_state_files() -> None:
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in settings.cluster_id)
    patterns = [
        f"hosted-coordinator-{safe or 'cluster'}*.json",
        "hosted-coordinator-cluster*.json",
    ]
    for pattern in patterns:
        for path in Path(".tmp").glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def hosted_coordinator_message(cluster_id: str, token: str, port: int) -> str:
    urls = local_coordinator_urls(port)
    url_lines = "\n".join(f"- `{url}`" for url in urls)
    primary_url = urls[0]
    return (
        "## Coordinator Hosted From This Machine\n\n"
        f"Cluster: `{cluster_id}`  \n"
        f"Token: `{token}`\n\n"
        "Other machines should use one of these coordinator URLs:\n\n"
        f"{url_lines}\n\n"
        "Connection pack:\n\n"
        "```text\n"
        f"Coordinator URL: {primary_url}\n"
        f"Health Check: {primary_url}/health\n"
        f"Cluster ID: {cluster_id}\n"
        f"Token: {token}\n"
        "```\n\n"
        "On each other app instance, click **Connect to Coordinator** or run `/connect-coordinator`, "
        "paste one of the URLs, then use the same cluster ID and token.\n\n"
        f"This machine's local runtime config was saved to `{RUNTIME_CONFIG_PATH}`. "
        "This UI session is now using the hosted coordinator."
    )


async def try_auto_host_coordinator(exc: CoordinationError) -> bool:
    if settings.coordination_backend.lower().strip() != "http":
        return False
    if not settings.coordinator_auto_host:
        return False
    if not settings.coordination_token:
        await cl.Message(content=f"Coordinator unavailable and auto-host is disabled without a token: {exc}").send()
        return False
    await cl.Message(
        content=(
            "Saved coordinator URLs are unavailable. Auto-host fallback is enabled, so this machine "
            "is starting a local coordinator."
        )
    ).send()
    started = await start_hosted_coordinator(
        settings.cluster_id,
        settings.coordination_token,
        settings.coordinator_port,
    )
    if not started:
        return False
    fallback_urls = local_coordinator_urls(settings.coordinator_port)
    coordination.http_urls = [*fallback_urls, *[url for url in coordination.http_urls if url not in fallback_urls]]
    coordination.http_url = coordination.http_urls[0]
    save_runtime_env(
        {
            "COORDINATION_BACKEND": "http",
            "COORDINATION_HTTP_URL": coordination.http_url,
            "COORDINATION_HTTP_URLS": ",".join(coordination.http_urls),
            "CLUSTER_ID": settings.cluster_id,
            "COORDINATION_TOKEN": settings.coordination_token,
            "COORDINATOR_AUTO_HOST": "true",
            "COORDINATOR_HOST": "0.0.0.0",
            "COORDINATOR_PORT": str(settings.coordinator_port),
        }
    )
    return True


async def start_hosted_coordinator(
    cluster_id: str,
    token: str,
    port: int,
    status_message: cl.Message | None = None,
    *,
    force_restart: bool = False,
) -> bool:
    global coordinator_process
    if coordinator_process and coordinator_process.poll() is None:
        if force_restart:
            stop_hosted_coordinator()
        else:
            return True
    state_path = hosted_state_path(cluster_id, port)
    if state_path.exists():
        state_path.unlink()
    coordinator_process = subprocess.Popen(
        [
            sys.executable,
            "scripts/run_coordinator.py",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--cluster-id",
            cluster_id,
            "--token",
            token,
            "--state-path",
            str(state_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_path = hosted_pid_path(cluster_id, port)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(coordinator_process.pid), encoding="utf-8")
    if not await wait_for_coordinator(port, cluster_id):
        if coordinator_process.poll() is None:
            coordinator_process.terminate()
        clear_hosted_pid_files()
        return False
    if coordinator_process.poll() is not None:
        clear_hosted_pid_files()
        return False
    token_error = await validate_coordinator_token(f"http://127.0.0.1:{port}", cluster_id, token)
    if token_error:
        if coordinator_process.poll() is None:
            coordinator_process.terminate()
        clear_hosted_pid_files()
        if status_message:
            status_message.content = (
                "Coordinator answered locally, but it did not accept the new token. "
                "An old coordinator may still be running on that port."
            )
            await status_message.update()
        return False
    if status_message:
        status_message.content = f"Coordinator is running locally on port `{port}`."
        await status_message.update()
    return True


async def wait_for_coordinator(port: int, cluster_id: str, timeout_seconds: float = 8.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/health"
    while asyncio.get_running_loop().time() < deadline:
        if coordinator_process and coordinator_process.poll() is not None:
            return False
        try:
            response = await asyncio.to_thread(httpx.get, url, timeout=1.5, trust_env=False)
            if response.status_code == 200 and response.json().get("cluster_id") == cluster_id:
                return True
        except (httpx.HTTPError, ValueError):
            await asyncio.sleep(0.3)
    return False


def parse_urls(text: str) -> list[str]:
    urls = []
    url_matches = re.findall(r"https?://[^\s,`]+|(?:\d{1,3}\.){3}\d{1,3}:\d+", text)
    raw_items = url_matches or text.replace("\n", ",").split(",")
    for item in raw_items:
        url = item.strip().strip("`").rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        if url and url not in urls:
            urls.append(url)
    return urls


def parse_connection_input(text: str) -> dict[str, object]:
    fields = {}
    for line in text.splitlines():
        separator = ":" if ":" in line else "=" if "=" in line else ""
        if not separator:
            continue
        key, value = line.split(separator, 1)
        normalized = key.strip().lower().replace("_", " ")
        fields[normalized] = value.strip().strip("`")
    return {
        "urls": parse_urls(
            fields.get("coordinator url", "")
            or fields.get("coordination http url", "")
            or fields.get("coordination http urls", "")
            or text
        ),
        "cluster_id": fields.get("cluster id", "") or fields.get("cluster", ""),
        "token": fields.get("token", "") or fields.get("coordination token", ""),
    }


async def fetch_coordinator_health(
    urls: list[str],
    timeout_seconds: float = 25.0,
    status_message: cl.Message | None = None,
) -> tuple[str, str] | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    attempts = 0
    last_error = ""
    while asyncio.get_running_loop().time() < deadline:
        for url in urls:
            attempts += 1
            try:
                response = await asyncio.to_thread(httpx.get, f"{url}/health", timeout=3, trust_env=False)
                if response.status_code == 200:
                    if status_message:
                        status_message.content = f"Coordinator reachable at `{url}`."
                        await status_message.update()
                    return url, str(response.json().get("cluster_id", ""))
                last_error = f"{url} returned HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = f"{url}: {exc}"
            except ValueError as exc:
                last_error = f"{url}: invalid health response ({exc})"
        if status_message:
            remaining = max(0, int(deadline - asyncio.get_running_loop().time()))
            status_message.content = (
                "Still checking coordinator reachability...\n\n"
                f"Attempts: `{attempts}`  \n"
                f"Last result: `{last_error or 'waiting'}`  \n"
                f"Time left: `{remaining}s`"
            )
            await status_message.update()
        await asyncio.sleep(2)
    return None


async def validate_coordinator_token(url: str, cluster_id: str, token: str) -> str:
    try:
        response = await asyncio.to_thread(
            httpx.get,
            f"{url}/state",
            headers={"Authorization": f"Bearer {token}"},
            params={"cluster_id": cluster_id},
            timeout=4,
            trust_env=False,
        )
    except httpx.HTTPError as exc:
        return str(exc)
    if response.status_code == 200:
        return ""
    return f"{response.status_code} {response.text}"


def prioritize_url(urls: list[str], live_url: str) -> list[str]:
    return [live_url, *[url for url in urls if url != live_url]]


def find_available_port(preferred_port: int, attempts: int = 20) -> int:
    for port in range(preferred_port, preferred_port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    return preferred_port


def coordinator_reachability_help(urls: list[str]) -> str:
    url_lines = "\n".join(f"- `{url}/health`" for url in urls)
    return (
        "## Coordinator Not Reachable Yet\n\n"
        "I retried for about 25 seconds and still could not reach the coordinator.\n\n"
        "Try opening this from the connecting machine's browser:\n\n"
        f"{url_lines}\n\n"
        "If it does not load, check:\n\n"
        "- The host machine is still running **Host Coordinator**.\n"
        "- The URL uses the host's real Wi-Fi/LAN IP, not a virtual adapter IP.\n"
        "- The host firewall allows inbound TCP on that port.\n"
        "- Both machines are on the same network and Wi-Fi client isolation is off.\n\n"
        "If port `8765` is busy, host again; the app can choose the next available port and show a new connection pack."
    )


def apply_http_connection(
    urls: list[str],
    cluster_id: str,
    token: str,
    auto_host: bool | None = None,
) -> None:
    settings.coordination_backend = "http"
    settings.coordination_http_url = urls[0]
    settings.coordination_http_urls = ",".join(urls)
    settings.cluster_id = cluster_id
    settings.coordination_token = token
    if auto_host is not None:
        settings.coordinator_auto_host = auto_host
    coordination.backend = "http"
    coordination.http_urls = urls
    coordination.http_url = urls[0]
    coordination.cluster_id = cluster_id
    coordination.coordination_token = token
    coordination.token_hash = coordination._hash_token(token)


def apply_local_file_connection() -> None:
    settings.coordination_backend = "file"
    settings.coordination_http_url = ""
    settings.coordination_http_urls = ""
    settings.coordination_token = ""
    settings.cluster_id = "local"
    settings.coordinator_auto_host = False
    coordination.backend = "file"
    coordination.http_urls = []
    coordination.http_url = ""
    coordination.cluster_id = "local"
    coordination.coordination_token = ""
    coordination.token_hash = ""


def is_yes(text: str) -> bool:
    return text.strip().lower() in {"y", "yes", "true", "1", "on"}


def restart_required_message(title: str) -> str:
    return (
        f"## {title}\n\n"
        f"Saved to `{RUNTIME_CONFIG_PATH}`.\n\n"
        "Restart the UI/worker for the new connection settings to take effect. "
        "Use `q` or Ctrl-C in the terminal for a clean shutdown."
    )
