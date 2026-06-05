from __future__ import annotations

import atexit
import asyncio
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

from chat_orchestrate.backends import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    OPEN_SWARM_BACKEND,
    SIMULATED_BACKEND,
    backend_availability,
    backend_execution_hint,
    command_for_backend,
    command_is_runnable,
    detect_agent_backends,
    launch_backend_app,
    run_task,
)
from chat_orchestrate.config import get_settings
from chat_orchestrate.coordination import CoordinationError, CoordinationManager
from chat_orchestrate.models import OrchestrationRun
from chat_orchestrate.orchestrator import Orchestrator
from chat_orchestrate.project_space import ProjectSpaceError, ProjectSpaceManager
from chat_orchestrate.runtime_config import RUNTIME_CONFIG_PATH, clear_runtime_env, save_runtime_env
from chat_orchestrate.swarm_client import build_swarm_client
from chat_orchestrate.ui_state import (
    append_chat,
    clear_chat_history,
    load_chat_history,
    load_preferences,
    save_preferences,
)

settings = get_settings()
agent_backends = detect_agent_backends(settings.configured_backends, settings.command_overrides)
agent_roles = [*settings.default_agents, "backend", "frontend"]
agent_roles = list(dict.fromkeys(agent_roles))
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
            f"Ready. Active project space: {active}\n\n"
            f"Machine: `{local_node.machine_id}` as `{local_node.role}`\n"
            f"Orchestrator: `{orchestrator_node.machine_id}`\n\n"
            f"Cluster: `{settings.cluster_id}`\n"
            f"Coordination: `{settings.coordination_backend}`\n"
            f"Backends: `{', '.join(agent_backends)}`\n\n"
            "Commands: `/spaces`, `/use <name>`, `/create-space <name> <path>`, "
            "`/worktree <name> <repo-path> <branch>`, `/clone <name> <git-url> [branch]`, "
            "`/workspace-modes`, `/machines`, "
            "`/claim-orchestrator`, `/release-orchestrator`, `/tasks`, `/backends`, "
            "`/connect`, `/host-coordinator`, `/connect-coordinator`, `/connect-file`, "
            "`/end-session`, `/clear-history`."
        )
    ).send()
    await update_cluster_roster()
    await show_machines()


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

    try:
        local_node = coordination.heartbeat()
        orchestrator_node = coordination.get_or_elect_orchestrator()
    except CoordinationError as exc:
        await cl.Message(content=f"Coordination error: {exc}").send()
        return
    await update_cluster_roster()
    status = cl.Message(
        content=(
            f"Starting orchestration for `{project.name}`...\n\n"
            f"Local machine: `{local_node.machine_id}`\n"
            f"Orchestrator machine: `{orchestrator_node.machine_id}`"
        )
    )
    await status.send()

    selected_backend = cl.user_session.get("agent_backend") or "auto"
    refresh_advertised_backends(selected_backend)
    turn_orchestrator = Orchestrator(
        build_swarm_client(settings, selected_backend, load_command_overrides()),
        settings.default_agents,
        coordination,
        settings.delegated_task_wait_seconds,
    )
    async for event in turn_orchestrator.run(text, project):
        if isinstance(event, OrchestrationRun):
            append_chat("assistant", "Run Summary", event.final)
            await cl.Message(content=event.final).send()
            continue

        append_chat("assistant", event.agent, event.content)
        await cl.Message(
            author=event.agent,
            content=f"**{event.role}**\n\n{event.content}",
        ).send()


@cl.on_settings_update
async def on_settings_update(updated_settings: dict) -> None:
    backend = str(updated_settings.get("agent_backend", "auto"))
    restore_history = bool(updated_settings.get("restore_history", True))
    codex_command = validated_command_preference(
        CODEX_BACKEND,
        clean_command_input(updated_settings.get("codex_command", "")),
    )
    claude_command = validated_command_preference(
        CLAUDE_CODE_BACKEND,
        clean_command_input(updated_settings.get("claude_command", "")),
    )
    cl.user_session.set("agent_backend", backend)
    cl.user_session.set("restore_history", restore_history)
    cl.user_session.set("codex_command", codex_command)
    cl.user_session.set("claude_command", claude_command)
    refresh_advertised_backends(backend)
    save_preferences(
        {
            "agent_backend": backend,
            "restore_history": str(restore_history).lower(),
            "codex_command": codex_command,
            "claude_command": claude_command,
        }
    )
    await update_cluster_roster()


async def handle_command(text: str) -> None:
    parts = text.split()
    command = parts[0].lower()

    try:
        if command == "/spaces":
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


async def setup_chat_settings() -> None:
    preferences = load_preferences()
    backend_values = [
        "auto",
        CODEX_BACKEND,
        CLAUDE_CODE_BACKEND,
        OPEN_SWARM_BACKEND,
        SIMULATED_BACKEND,
        *agent_backends,
    ]
    unique_backend_values = []
    for backend in backend_values:
        if backend not in unique_backend_values:
            unique_backend_values.append(backend)
    selected_backend = preferences.get("agent_backend", "auto")
    if selected_backend not in unique_backend_values:
        selected_backend = "auto"
    restore_history = preferences.get("restore_history", "true").lower() != "false"
    codex_command = validated_command_preference(
        CODEX_BACKEND,
        clean_command_input(preferences.get("codex_command", settings.codex_command)),
    )
    claude_command = validated_command_preference(
        CLAUDE_CODE_BACKEND,
        clean_command_input(preferences.get("claude_command", settings.claude_command)),
    )
    codex_initial = codex_command or command_for_backend(CODEX_BACKEND, settings.command_overrides) or ""
    claude_initial = claude_command or command_for_backend(CLAUDE_CODE_BACKEND, settings.command_overrides) or ""
    cl.user_session.set("agent_backend", selected_backend)
    cl.user_session.set("restore_history", restore_history)
    cl.user_session.set("codex_command", codex_initial)
    cl.user_session.set("claude_command", claude_initial)
    refresh_advertised_backends(selected_backend)
    save_preferences(
        {
            "codex_command": codex_command,
            "claude_command": claude_command,
        }
    )
    await cl.ChatSettings(
        [
            Select(
                id="agent_backend",
                label="Local Agent",
                values=unique_backend_values,
                initial=selected_backend,
                tooltip="Choose which locally installed agent CLI should answer chat turns.",
            ),
            Switch(
                id="restore_history",
                label="Restore local chat on refresh",
                initial=restore_history,
                tooltip="Replay recent local chat records when the page reconnects.",
            ),
            TextInput(
                id="codex_command",
                label="Codex Command",
                initial=codex_initial,
                placeholder="codex, codex.cmd, or full path",
                tooltip="Command used when Local Agent is codex.",
            ),
            TextInput(
                id="claude_command",
                label="Claude Command",
                initial=claude_initial,
                placeholder="claude, claude.cmd, or full path",
                tooltip="Command used when Local Agent is claude-code.",
            ),
        ]
    ).send()


def refresh_advertised_backends(selected_backend: str = "auto") -> None:
    global agent_backends
    detected = detect_agent_backends(settings.configured_backends, load_command_overrides())
    advertised = []
    for backend in [*detected, selected_backend]:
        if backend and backend != "auto" and backend not in advertised:
            advertised.append(backend)
    if not advertised:
        advertised = [SIMULATED_BACKEND]
    agent_backends = advertised
    coordination.agent_backends = advertised


def load_command_overrides() -> dict[str, str]:
    try:
        codex_command = cl.user_session.get("codex_command")
        claude_command = cl.user_session.get("claude_command")
    except Exception:
        codex_command = None
        claude_command = None
    preferences = load_preferences()
    return {
        CODEX_BACKEND: clean_command_input(codex_command or preferences.get("codex_command", settings.codex_command)),
        CLAUDE_CODE_BACKEND: clean_command_input(
            claude_command or preferences.get("claude_command", settings.claude_command)
        ),
    }


def validated_command_preference(backend: str, command: str) -> str:
    if command and command_is_runnable(backend, command, settings.command_overrides):
        return command
    return command_for_backend(backend, settings.command_overrides) or ""


def clean_command_input(value: object) -> str:
    clean = str(value or "").strip().strip('"').strip("'")
    return "" if clean.lower() in {"none", "null", "undefined"} else clean


async def restore_chat_history() -> None:
    if not cl.user_session.get("restore_history", True):
        return
    history = load_chat_history()
    if not history:
        return
    await cl.Message(content="## Restored Local Chat\n\nRecent local messages from this machine:").send()
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
        await cl.Message(content=f"Coordinator token check failed: {validation_error}").send()
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
    if coordinator_process and coordinator_process.poll() is None:
        cluster_id = hosted_connection.get("cluster_id", settings.cluster_id)
        token = hosted_connection.get("token", settings.coordination_token)
        port = int(hosted_connection.get("port", str(settings.coordinator_port)))
        await cl.Message(
            content=hosted_coordinator_message(cluster_id, token, port)
        ).send()
        return

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


@cl.action_callback("clear_history")
async def clear_history_action(_: cl.Action) -> None:
    clear_chat_history()
    await cl.Message(content="Local restored chat history cleared.").send()


def machine_actions() -> list[cl.Action]:
    return [
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
            return
        except Exception:
            pass

    message = cl.Message(content=content, actions=cluster_roster_actions())
    await message.send()
    cl.user_session.set("cluster_roster_message", message)


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
        return str(selected)
    return load_preferences().get("agent_backend", "auto")


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
    items = backend_availability(agent_backends, load_command_overrides())
    lines = []
    for item in items:
        state = "headless-ready" if item.available else "installed-app-only" if item.installed_app else "not-ready"
        command = f" `{item.command}`" if item.command else ""
        app = f"\n> App: `{item.installed_app}`" if item.installed_app else ""
        hint = "" if item.available else f"\n> {backend_execution_hint(item.name)}"
        lines.append(f"> ### {item.name}\n> `{state}`{command}{app}{hint}\n")
    return "## Agent Backends\n\n" + "\n".join(lines)


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


def hosted_state_path(cluster_id: str) -> Path:
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in cluster_id)
    return Path(".tmp") / f"hosted-coordinator-{safe or 'cluster'}.json"


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
) -> bool:
    global coordinator_process
    if coordinator_process and coordinator_process.poll() is None:
        return True
    state_path = hosted_state_path(cluster_id)
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
    if not await wait_for_coordinator(port, cluster_id):
        if coordinator_process.poll() is None:
            coordinator_process.terminate()
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
            response = await asyncio.to_thread(httpx.get, url, timeout=1.5)
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
                response = await asyncio.to_thread(httpx.get, f"{url}/health", timeout=3)
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
