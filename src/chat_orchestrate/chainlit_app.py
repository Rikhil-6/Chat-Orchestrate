from __future__ import annotations

import atexit
import asyncio
import functools
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import chainlit as cl
import httpx
from chainlit.input_widget import Select, Switch, TextInput

from chat_orchestrate.a2a import A2A_PROTOCOL_VERSION
from chat_orchestrate.artifacts import (
    artifacts_markdown,
    build_evaluation,
    build_evaluation_summary,
    preview_backend_url,
    preview_command,
    preview_frontend_url,
    save_project_preview_ports,
    scan_project_artifacts,
    task_completion_stats,
    work_proof_summary,
    workspace_layout_markdown,
)
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
    task_workspace_path,
)
from chat_orchestrate.capabilities import infer_machine_capabilities
from chat_orchestrate.config import get_settings
from chat_orchestrate.coordination import CoordinationError, CoordinationManager
from chat_orchestrate.models import AgentSpec, DelegatedTask, MachineNode, OrchestrationRun, ProgressUpdate, ProjectSpace
from chat_orchestrate.orchestrator import Orchestrator
from chat_orchestrate.project_space import (
    ProjectSpaceError,
    ProjectSpaceManager,
    parse_project_share_pack,
    project_name_from_remote,
    project_share_pack,
)
from chat_orchestrate.runtime_config import RUNTIME_CONFIG_PATH, clear_runtime_env, save_runtime_env
from chat_orchestrate.summaries import summarize_goal
from chat_orchestrate.swarm_client import build_swarm_client, is_benign_agent_stderr, workspace_write_contract
from chat_orchestrate.ui_state import (
    append_chat,
    archive_chat_thread,
    chat_thread_state,
    clear_chat_history,
    create_chat_thread,
    load_chat_history,
    load_credentials,
    load_preferences,
    rename_chat_thread,
    save_credentials,
    save_preferences,
    set_chat_thread_project,
    set_active_chat_thread,
)

LOGGER = logging.getLogger(__name__)

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
    settings.task_lease_seconds,
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

        await run_claimed_ui_task(task)


async def run_claimed_ui_task(task) -> None:
    status_message = cl.Message(content=worker_task_status_content(task, "assigned"))
    try:
        await status_message.send()
    except Exception:
        status_message = None
    await show_dashboard_sidebar()

    try:
        try:
            coordination.note_task_progress(
                task.task_id,
                f"Claimed by `{coordination.machine_id}`. Starting `{task.role}` work: {task.brief or task.title}",
                status="running",
            )
        except CoordinationError:
            pass
        if settings.use_local_agent_chat and task.preferred_backend != SIMULATED_BACKEND:
            result = await run_claimed_ui_task_live(task, status_message)
        else:
            result = await run_claimed_ui_task_buffered(task, status_message)
    except Exception as exc:  # pragma: no cover - defensive UI worker boundary
        try:
            coordination.complete_task(task.task_id, str(exc), status="failed")
        except CoordinationError:
            pass
        if status_message is not None:
            status_message.content = worker_task_status_content(task, "failed", result=str(exc))
            try:
                await status_message.update()
            except Exception:
                pass
    else:
        try:
            coordination.complete_task(task.task_id, result)
        except CoordinationError:
            pass
        if status_message is not None:
            status_message.content = worker_task_status_content(task, "completed", result=result)
            try:
                await status_message.update()
            except Exception:
                pass
    await show_dashboard_sidebar()


async def run_claimed_ui_task_buffered(task, status_message: cl.Message | None) -> str:
    work = asyncio.create_task(
        asyncio.to_thread(
            run_task,
            task,
            not settings.use_local_agent_chat,
            load_command_overrides(),
            load_openai_api_key(),
            settings.codex_api_model,
            settings.workspaces_root,
        )
    )
    started = asyncio.get_running_loop().time()
    tick = 0
    while not work.done():
        await asyncio.sleep(max(2.0, min(5.0, settings.worker_poll_seconds)))
        if work.done():
            break
        tick += 1
        await update_worker_status_message(
            status_message,
            task,
            "running",
            int(asyncio.get_running_loop().time() - started),
            tick,
        )
        await show_dashboard_sidebar()
    return await work


async def run_claimed_ui_task_live(task, status_message: cl.Message | None) -> str:
    workspace_path = task_workspace_path(task, settings.workspaces_root) or (settings.workspaces_root / task.project)
    workspace_path.mkdir(parents=True, exist_ok=True)
    project = ProjectSpace(name=task.project, path=workspace_path)
    agent = AgentSpec(
        name=task.role.replace("-", " ").title(),
        role=task.role,
        instructions=f"Handle the `{task.role}` slice of this distributed project run.",
    )
    goal = (
        f"You are the `{task.role}` agent for a distributed project run.\n\n"
        f"Task: {task.title}\n"
        f"Project space: {task.project}\n"
        f"Project path: {workspace_path}\n\n"
        f"{workspace_write_contract(project)}\n\n"
        f"Goal:\n{task.goal}\n\n"
        "Work concretely in the project space when possible. Surface blockers, sandbox/tool errors, "
        "files changed, commands run, and preview or verification details."
    )
    client = build_swarm_client(
        settings,
        task.preferred_backend,
        load_command_overrides(),
        load_openai_api_key(),
        load_agent_api_keys(),
    )
    started = asyncio.get_running_loop().time()
    tick = 0
    result = ""
    async for event in client.run_agent_events(agent, project, goal, ""):
        if isinstance(event, ProgressUpdate):
            tick += 1
            await update_worker_status_message(
                status_message,
                task,
                "running",
                int(asyncio.get_running_loop().time() - started),
                tick,
                event.message,
            )
            await show_dashboard_sidebar()
            continue
        result = event
    return result


async def update_worker_status_message(
    status_message: cl.Message | None,
    task,
    phase: str,
    elapsed: int = 0,
    tick: int = 0,
    result: str = "",
) -> None:
    if status_message is None:
        return
    status_message.content = worker_task_status_content(task, phase, elapsed, tick, result)
    if phase == "running":
        note = result or worker_running_note(task, tick, elapsed)
        try:
            coordination.note_task_progress(task.task_id, note[:240], status="running")
        except CoordinationError:
            pass
    try:
        await status_message.update()
    except Exception:
        pass


def worker_task_status_content(task, phase: str, elapsed: int = 0, tick: int = 0, result: str = "") -> str:
    heading = {
        "assigned": "Assigned To This Machine",
        "running": "Local Agent Working",
        "completed": "Task Completed",
        "failed": "Task Failed",
    }.get(phase, "Worker Task")
    lines = [
        f"## {heading}",
        "",
        f"Role: `{task.role}`",
        f"Backend: `{task.preferred_backend}`",
        f"Project: `{task.project}`",
        f"Task: {task.title}",
        f"Brief: {task.brief or task.title}",
    ]
    if phase == "assigned":
        lines.append("")
        lines.append(
            f"This machine now owns the `{task.role}` slice for `{task.project}` and should execute: "
            f"{task.brief or task.title}"
        )
        lines.append("The local agent is starting now, and its output should come back through this chat.")
    elif phase == "running":
        lines.append("")
        lines.append(f"Status: {worker_running_note(task, tick, elapsed)}")
        if result:
            lines.append("")
            lines.append(f"Latest agent signal: {first_content_line(result)[:360]}")
    elif phase == "completed":
        lines.append("")
        lines.append("Returned the result to the coordinator.")
        summary = first_content_line(result)
        if summary and summary != "No output captured.":
            lines.append(f"Result: {summary}")
    elif phase == "failed":
        lines.append("")
        lines.append("The coordinator has been told this task failed.")
        if result:
            lines.append(f"Error: {result[:240]}")
    return "\n".join(lines)


def worker_running_note(task, tick: int, elapsed: int) -> str:
    activities = [
        f"Opening `{task.project}` and checking the local files needed for `{task.role}`.",
        f"Running `{task.preferred_backend}` on this machine for: {task.brief or task.title}",
        f"Packaging changed files, preview steps, and notes for the `{task.role}` handoff.",
    ]
    return f"{activities[(max(1, tick) - 1) % len(activities)]} Elapsed `{elapsed}s`."


def ensure_project_space(name: str) -> ProjectSpace:
    clean_name = slugify_project_name(name or "default")
    try:
        return spaces.get(clean_name)
    except ProjectSpaceError:
        return spaces.upsert(clean_name, clean_name)


def initial_project_space() -> ProjectSpace:
    preferences = load_preferences()
    preferred = str(preferences.get("project_name", "")).strip()
    if preferred:
        return ensure_project_space(preferred)

    existing = spaces.list_spaces()
    if existing:
        return existing[0]
    return ensure_project_space("default")


def initial_project_space_for_chat(chat_state: dict) -> ProjectSpace:
    project_name = str(chat_state.get("active_project_name", "")).strip()
    if project_name:
        return ensure_project_space(project_name)
    return initial_project_space()


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("last_goal", "")
    cl.user_session.set("last_run", None)
    chat_state = chat_thread_state()
    cl.user_session.set("active_chat_id", chat_state["active_id"])
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
    project = initial_project_space_for_chat(chat_state)
    cl.user_session.set("project_space", project)
    active = f"`{project.name}`"
    save_preferences({"project_name": project.name})
    set_chat_thread_project(chat_state["active_id"], project.name)

    await setup_chat_settings()
    start_ui_worker()
    await restore_chat_history(chat_state["active_id"])
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
    maybe_rename_empty_active_chat(text)
    append_chat("user", "You", text)

    status_follow_up = active_run_status_response(text)
    if status_follow_up:
        append_chat("assistant", "Assistant", status_follow_up)
        await cl.Message(content=status_follow_up).send()
        await show_dashboard_sidebar()
        return

    # Normal chat text is always handed to the selected agent/model path.
    # Dashboard classification and progress are telemetry; slash commands are the control plane.
    cl.user_session.set("last_goal", text)
    selected_backend = normalize_selected_backend(str(cl.user_session.get("agent_backend") or "auto"))
    refresh_advertised_backends(selected_backend, text)
    if not await ensure_selected_backend_ready(selected_backend):
        return

    progress_items: dict[str, str] = {
        "preflight:start": "- `intake` Preparing the coordinator and live machine roster."
    }
    progress_message = cl.Message(content=render_progress(progress_items))
    await progress_message.send()
    try:
        await run_status_call(
            "preflight:heartbeat",
            "coordinator-check",
            "Contacting the shared coordinator and refreshing this machine heartbeat.",
            coordination.heartbeat,
            progress_items,
            progress_message,
        )
        await run_status_call(
            "preflight:election",
            "orchestrator-check",
            "Confirming which machine currently owns orchestration.",
            coordination.get_or_elect_orchestrator,
            progress_items,
            progress_message,
        )
    except CoordinationError as exc:
        progress_items["preflight:error"] = f"- `error` Coordination error before the run could start: `{exc}`"
        progress_message.content = render_progress(progress_items)
        await progress_message.update()
        return

    turn_agent_names = ["coordinator"]
    turn_orchestrator = Orchestrator(
        build_swarm_client(
            settings,
            selected_backend,
            load_command_overrides(),
            load_openai_api_key(),
            load_agent_api_keys(),
        ),
        turn_agent_names,
        coordination,
        delegated_task_wait_seconds=settings.delegated_task_wait_seconds,
        delegated_task_ack_seconds=settings.delegated_task_ack_seconds,
        conversation_context=recent_chat_context(text),
    )
    turns = []
    final_run = None
    async for event in turn_orchestrator.run(text, project):
        if isinstance(event, ProgressUpdate):
            if should_hide_progress_update(event):
                continue
            progress_items[progress_key(event)] = progress_line(event)
            progress_message.content = render_progress(progress_items)
            await progress_message.update()
            if event.phase in {"delegation", "assigned", "coordinator-ready", "handoff", "remote-work", "artifact-check"}:
                await show_dashboard_sidebar()
            continue
        if isinstance(event, OrchestrationRun):
            final_run = event
            continue

        turns.append(event)
        if not should_hide_turn_completion(event):
            progress_items[f"done:{event.agent}:{event.role}"] = (
                f"- `done` {event.agent} finished its `{event.role}` pass."
            )
            progress_message.content = render_progress(progress_items)
            await progress_message.update()
        await show_dashboard_sidebar()

    if final_run:
        cl.user_session.set("last_run", final_run)
    progress_message.content = "## Coordination Status\n\n- `ready` Response is ready. Live routing and artifact details remain in the dashboard."
    await progress_message.update()
    response = conversational_response(final_run, turns)
    append_chat("assistant", "Assistant", response)
    await cl.Message(content=response).send()
    await update_cluster_roster()


def active_run_status_response(text: str) -> str:
    run = cl.user_session.get("last_run")
    if not run:
        return ""
    if not looks_like_run_status_prompt(text):
        return ""
    tasks = live_response_tasks(run)
    if not tasks:
        return ""
    active = [task for task in tasks if task.status in {"delegated", "running"}]
    recent = active or tasks[:3]
    lines = ["Here’s the live assignment status for this run:", ""]
    for task in recent[:4]:
        note = task.progress_note or task.brief or task.title
        lines.append(
            f"- `{task.role}` on `{task.assigned_machine}` via `{task.preferred_backend}` is `{task.status}`. {note}"
        )
    completed = [task for task in tasks if task.status == "completed"]
    failed = [task for task in tasks if task.status == "failed"]
    lines.append("")
    lines.append(
        f"Summary: `{len(completed)}/{len(tasks)}` completed"
        + (f", `{len(active)}` active" if active else "")
        + (f", `{len(failed)}` failed" if failed else "")
        + "."
    )
    return "\n".join(lines)


def looks_like_run_status_prompt(text: str) -> bool:
    clean = " ".join(str(text or "").lower().split())
    if not clean:
        return False
    explicit = [
        "status",
        "progress",
        "update",
        "assigned",
        "claimed",
        "so task",
        "what task",
        "what's happening",
        "whats happening",
        "still running",
        "who is doing",
        "who's doing",
        "when the task has been assigned",
        "let me know when the task has been assigned",
        "what tasks are to be done",
        "what task is to be done",
        "what needs to be done",
        "what's left",
        "whats left",
        "to do here",
        "todo here",
        "what to do here",
        "what should be done here",
    ]
    if any(marker in clean for marker in explicit):
        return True
    if len(clean) <= 48 and any(
        term in clean for term in ["task", "tasks", "todo", "to do", "assigned", "update", "progress", "left"]
    ):
        return True
    return False


def recent_chat_context(current_message: str, limit: int = 8) -> str:
    chat_id = str(cl.user_session.get("active_chat_id") or "")
    records = load_chat_history(limit=limit, chat_id=chat_id or None)
    if records and records[-1].role == "user" and records[-1].content.strip() == current_message.strip():
        records = records[:-1]
    if not records:
        return ""
    lines = ["Recent chat before the latest user message:"]
    for record in records[-limit:]:
        speaker = "User" if record.role == "user" else "Assistant"
        content = " ".join(record.content.strip().split())
        if len(content) > 600:
            content = f"{content[:597]}..."
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def parse_preview_port_change(text: str) -> dict[str, int]:
    lowered = text.lower()
    if "port" not in lowered and not re.search(r"\b(5173|8000)\b", lowered):
        return {}
    if not re.search(r"\b(use|using|swap|switch|change|move|set|try|take|taken|busy|occupied|free|available|instead)\b", lowered):
        return {}

    ports = [int(match) for match in re.findall(r"\b([1-9]\d{2,4})\b", lowered)]
    ports = [port for port in ports if 1 <= port <= 65535]
    if not ports:
        return {}

    change: dict[str, int] = {}
    frontend_match = re.search(r"\b(?:front\s*end|frontend|ui|site|preview)\b[^\d]{0,40}([1-9]\d{2,4})\b", lowered)
    backend_match = re.search(r"\b(?:back\s*end|backend|api|server)\b[^\d]{0,40}([1-9]\d{2,4})\b", lowered)
    if frontend_match:
        frontend_port = int(frontend_match.group(1))
        if 1 <= frontend_port <= 65535:
            change["frontend_port"] = frontend_port
    if backend_match:
        backend_port = int(backend_match.group(1))
        if 1 <= backend_port <= 65535:
            change["backend_port"] = backend_port

    if change:
        return change
    if "backend" in lowered or "api" in lowered or "server" in lowered:
        return {"backend_port": ports[-1]}
    return {"frontend_port": ports[-1]}


def apply_preview_port_change(project: ProjectSpace, change: dict[str, int]) -> str:
    save_project_preview_ports(
        project,
        frontend_port=change.get("frontend_port"),
        backend_port=change.get("backend_port"),
    )
    lines = ["### Preview Port Updated", f"Project: `{project.name}`"]
    if "frontend_port" in change:
        lines.append(f"Frontend preview now opens at `{preview_frontend_url(project)}`.")
    if "backend_port" in change:
        lines.append(f"Backend API health now opens at `{preview_backend_url(project)}`.")
    lines.extend(
        [
            "",
            "Run the preview with:",
            "",
            "```powershell",
            preview_command(project),
            "```",
        ]
    )
    return "\n".join(lines)


@cl.on_settings_update
async def on_settings_update(updated_settings: dict) -> None:
    backend = normalize_selected_backend(str(updated_settings.get("agent_backend", "Select")))
    restore_history = bool(updated_settings.get("restore_history", True))
    project_name = slugify_project_name(str(updated_settings.get("project_name", "") or "default"))
    current_project = activate_project_space(project_name)
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
            "project_name": current_project.name,
            "restore_history": str(restore_history).lower(),
            "codex_command": codex_command,
            "claude_command": claude_command,
            "gemini_command": gemini_command,
        }
    )
    await setup_chat_settings(backend)
    await update_cluster_roster()
    await show_dashboard_sidebar()


def conversational_response(run: OrchestrationRun | None, turns: list) -> str:
    goal = run.goal if run else str(cl.user_session.get("last_goal") or "")
    project = run.project if run else cl.user_session.get("project_space")
    response_tasks = live_response_tasks(run)
    proof = compact_work_proof(project, response_tasks)
    should_attach_proof = should_attach_supporting_workspace(goal, run)

    if not turns:
        if proof and should_attach_proof:
            return f"The selected agent did not return a chat answer, but generated artifacts are available.{supporting_suffix(proof)}"
        return "The selected agent did not return a chat answer yet, and I could not find generated workspace artifacts."

    content = best_agent_answer(turns)
    diagnostic = agent_failure_diagnostic(turns, project)
    if diagnostic and agent_output_is_failure(content) and not agent_output_has_successful_fallback(turns):
        content = diagnostic
    if not content:
        content = diagnostic
    if not content:
        content = agent_no_answer_message(project, proof)

    if run and run.delegated_tasks:
        if agent_setup_failure(content):
            return routing_response_with_setup_issue(run, content)
        if worker_result_needs_recovery(content):
            return content
        return f"{content}{supporting_suffix(proof) if proof and should_attach_proof else ''}"
    if proof and should_attach_proof:
        return f"{content}{supporting_suffix(proof)}"
    return content


def should_attach_supporting_workspace(goal: str, run: OrchestrationRun | None) -> bool:
    lowered = " ".join(str(goal or "").lower().split())
    explicit_artifact_request = any(
        marker in lowered
        for marker in [
            "where",
            "code",
            "workspace",
            "files",
            "proof",
            "preview",
            "artifact",
            "generated",
            "did work",
            "show me",
        ]
    )
    if explicit_artifact_request:
        return True
    if not run:
        return False
    return any(task.role != "coordinator" for task in run.delegated_tasks)


def best_agent_answer(turns: list) -> str:
    fallback = successful_agent_fallback_answer(turns)
    if fallback:
        return fallback
    chosen = choose_answer_turn(turns)
    content = full_agent_chat_content(chosen.content)
    if answer_is_generic(content):
        substantive = next(
            (
                full_agent_chat_content(turn.content)
                for turn in reversed(turns)
                if not answer_is_generic(full_agent_chat_content(turn.content))
            ),
            "",
        )
        return substantive or content
    return content


def successful_agent_fallback_answer(turns: list) -> str:
    for turn in reversed(turns):
        raw = str(getattr(turn, "content", "") or "")
        lowered = raw.lower()
        if "api fallback response" not in lowered and "api response" not in lowered:
            continue
        clean = full_agent_chat_content(raw)
        if clean and not agent_output_is_failure(clean):
            return clean
    return ""


def agent_output_has_successful_fallback(turns: list) -> bool:
    return bool(successful_agent_fallback_answer(turns))


def agent_failure_diagnostic(turns: list, project: ProjectSpace | None = None) -> str:
    raw = "\n".join(str(getattr(turn, "content", "") or "") for turn in turns)
    lowered = raw.lower()
    if not lowered.strip():
        return ""
    workspace_line = f"\n\nActive workspace: `{project.path.resolve()}`" if project else ""
    if "failed to open state db" in lowered or "readonly database" in lowered or "state_db" in lowered:
        return (
            "The selected Codex backend started, but Codex failed before it could return a final answer because "
            "its local state database is not writable from this app process. This is a local Codex session/access "
            "problem, not a project-layout problem."
            f"{workspace_line}\n\n"
            "Fix: save an OpenAI API key in Settings for automatic Codex API fallback, or restart Chat Orchestrate "
            "from a normal terminal session that can run the same `codex exec` command and write to "
            f"`{Path.home() / '.codex'}`."
        )
    if "failed to initialize in-process app-server client" in lowered:
        return (
            "The selected Codex backend launched, but Codex could not initialize its local app-server client. "
            "The harness did not invent a replacement answer."
            f"{workspace_line}\n\n"
            "Fix: open/sign in to Codex normally, then restart Chat Orchestrate from that same working terminal session."
        )
    if "claude" in lowered and any(
        marker in lowered
        for marker in [
            "not logged in",
            "authentication failed",
            "auth failed",
            "auth error",
            "unauthorized",
            "missing api key",
            "invalid api key",
            "no api key",
            "permission denied",
            "access is denied",
        ]
    ):
        return (
            "The selected Claude Code backend started or was selected, but the local Claude session/CLI was not usable "
            "from this app process. If a Claude API key is saved in Settings, the harness will try that fallback "
            "automatically; otherwise it will surface the local CLI failure."
            f"{workspace_line}\n\n"
            "Fix: sign in through the normal `claude` CLI flow, set the Claude Code command path, or save a Claude "
            "API key in Settings."
        )
    if "gemini" in lowered and any(
        marker in lowered
        for marker in [
            "not logged in",
            "authentication failed",
            "auth failed",
            "auth error",
            "unauthorized",
            "missing api key",
            "invalid api key",
            "no api key",
            "permission denied",
            "access is denied",
        ]
    ):
        return (
            "The selected Gemini CLI backend started or was selected, but the local Gemini session/CLI was not usable "
            "from this app process. If a Gemini API key is saved in Settings, the harness will try that fallback "
            "automatically; otherwise it will surface the local CLI failure."
            f"{workspace_line}\n\n"
            "Fix: sign in through the normal `gemini` CLI flow, set the Gemini CLI command path, or save a Gemini "
            "API key in Settings."
        )
    if "exited with code" in lowered and "did not return final chat text" in lowered:
        return (
            "The selected local agent exited without returning final chat text. I kept the raw failure details in "
            "the run output instead of generating fake code."
            f"{workspace_line}\n\n"
            "Use `/layout` to confirm where project code should be written, then rerun after the local agent command "
            "works from a normal terminal."
        )
    if "did not receive a usable response" in lowered:
        return full_agent_chat_content(raw)
    return ""


def agent_output_is_failure(content: str) -> bool:
    lowered = " ".join(str(content or "").lower().split())
    return any(
        marker in lowered
        for marker in [
            "failed to open state db",
            "readonly database",
            "state_db",
            "failed to initialize in-process app-server client",
            "did not return final chat text",
            "did not receive a usable response",
            "exited with code",
            "not logged in",
            "authentication failed",
            "auth failed",
            "auth error",
            "unauthorized",
        ]
    )


def agent_no_answer_message(project: ProjectSpace | None, proof: str) -> str:
    workspace = f"`{project.path.resolve()}`" if project else "the active workspace"
    if proof:
        return (
            "The selected agent did not return chat text, but I found workspace artifacts. "
            f"Code evidence is under {workspace}."
        )
    return (
        "The selected agent returned no chat text and no new workspace artifacts were found. "
        f"Active workspace: {workspace}. Use `/layout` for the source/runtime split."
    )


def choose_answer_turn(turns: list):
    for turn in reversed(turns):
        content = full_agent_chat_content(turn.content)
        if content and not answer_is_generic(content):
            return turn
    return turns[-1]


def full_agent_chat_content(content: str, max_chars: int = 1800) -> str:
    clean = clean_agent_chat_content(content)
    if len(clean) <= max_chars:
        return clean
    shortened = clean[:max_chars].rsplit("\n", 1)[0].rstrip()
    return f"{shortened}\n\n..."


def answer_is_generic(content: str) -> bool:
    lowered = " ".join(str(content or "").lower().split())
    generic_markers = [
        "coordinator routing is ready",
        "response is ready",
        "details are tucked into the dashboard",
        "reviewed `",
        "preview fallback wrote workspace code",
        "done.",
    ]
    return not lowered or any(marker in lowered for marker in generic_markers)


def workspace_middleman_response(goal: str, project: ProjectSpace | None) -> str:
    lowered = goal.lower()
    if project is None:
        return "I found generated project artifacts, but the active workspace path is not loaded in this chat session."
    if any(marker in lowered for marker in ["where", "code", "workspace", "files", "did work", "proof"]):
        return f"The generated code for this run is in `{project.path}`. I’ll keep the exact files and preview command below."
    return "I’ve got a concrete workspace result for this run. The generated files and preview command are below."


def workspace_middleman_response(goal: str, project: ProjectSpace | None) -> str:
    lowered = goal.lower()
    if project is None:
        return "I found generated project artifacts, but the active workspace path is not loaded in this chat session."
    if any(marker in lowered for marker in ["where", "code", "workspace", "files", "did work", "proof"]):
        return f"The generated code for this run is in `{project.path}`. I will keep the exact files and preview command below."
    if looks_like_visual_feedback(lowered):
        return (
            f"This is frontend styling feedback. The active app files are in `{project.path / 'frontend'}`; "
            "the supporting proof below shows what can be previewed."
        )
    return "I have a concrete workspace result for this run. The generated files and preview command are below."


def looks_like_diagnostic_prompt(lowered_goal: str) -> bool:
    return any(
        marker in lowered_goal
        for marker in [
            "404",
            "not found",
            "error",
            "warning",
            "failed",
            "minor errors",
            "/api/",
            "backend health",
            "doesn't work",
            "doesnt work",
            "doesn't look",
            "doesnt look",
            "not quite",
            "color",
            "colour",
            "visual",
            "youtube-y",
        ]
    )


def looks_like_visual_feedback(lowered_goal: str) -> bool:
    return any(
        marker in lowered_goal
        for marker in [
            "color",
            "colour",
            "colors",
            "colours",
            "style",
            "styling",
            "visual",
            "youtube-y",
            "youtube like",
            "youtube-like",
            "not quite youtube",
            "doesn't seem",
            "doesnt seem",
        ]
    )


def compact_work_proof(project: ProjectSpace | None, tasks: object | None = None) -> str:
    if project is None:
        return ""
    artifacts = scan_project_artifacts(project, limit=5)
    task_list = list(tasks) if tasks and not isinstance(tasks, list) else (tasks or [])
    evaluation = build_evaluation_summary(project, task_list)
    lines = [
        "### Supporting Workspace",
        f"Workspace: `{project.path}`",
    ]
    if artifacts:
        files = ", ".join(f"`{artifact.relative_path}`" for artifact in artifacts[:5])
        lines.append(f"Files: {files}")
        if any(artifact.relative_path == "frontend/index.html" for artifact in artifacts):
            lines.append(f"Preview: `{preview_command(project)}`")
    stats = task_completion_stats(task_list)
    if stats["total"]:
        lines.append(f"Agent tasks: `{stats['completed']}/{stats['total']}` completed")
    parts = [part for part in [evaluation, "\n".join(lines)] if part]
    return "\n\n".join(parts)


def live_response_tasks(run: OrchestrationRun | None) -> list[DelegatedTask]:
    if not run:
        return []
    run_id = str(getattr(run, "run_id", "") or "")
    if run_id:
        live_tasks = tasks_for_run(dashboard_tasks_snapshot(), run_id)
        if live_tasks:
            return live_tasks
    return list(getattr(run, "delegated_tasks", []) or [])


def supporting_suffix(proof: str) -> str:
    return f"\n\n{proof}" if proof else ""


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
    return prefix + humanize_progress_message(update)


def humanize_progress_message(update: ProgressUpdate) -> str:
    message = str(update.message or "").strip()
    if update.phase not in {"agent-warning", "agent-error"}:
        return message
    if "stderr:" not in message.lower():
        return message
    issue = classify_agent_warning(message)
    if issue:
        return issue
    return message


def classify_agent_warning(message: str) -> str:
    lowered = str(message or "").lower()
    if "objectnotfound" in lowered or "commandnotfoundexception" in lowered or "`rg`" in lowered or "(rg" in lowered:
        return "Local agent hit a missing shell tool while working. The task can continue, but this machine is missing part of the expected CLI toolchain."
    if "apply_patch verification failed" in lowered:
        return "Local agent tried to patch a file, but the file contents had drifted from what it expected. This task likely needs a fresh reread before retrying."
    if "readonly database" in lowered or "state db" in lowered or "in-process app-server client" in lowered:
        return "Local agent started, but its local runtime state was not writable from this process."
    if "unauthorized" in lowered or "invalid admin api key" in lowered or "missing or invalid admin api key" in lowered:
        return "The generated app hit an API authorization/runtime warning during verification."
    if (
        "export function notfound" in lowered
        or "export function unauthorized" in lowered
        or "title: " in lowered
        or "throw notfound(" in lowered
        or "throw unauthorized(" in lowered
    ):
        return "The generated app logged runtime warnings while the agent was checking its output."
    return ""


def should_hide_progress_update(update: ProgressUpdate) -> bool:
    if update.phase != "agent-warning":
        return False
    message = str(update.message or "")
    if is_benign_agent_stderr(message):
        return True
    lowered = message.lower()
    noisy_runtime_markers = [
        "title: ",
        "export function notfound",
        "export function unauthorized",
        "missing or invalid admin api key",
        "invalid admin api key",
        "throw notfound(",
        "throw unauthorized(",
    ]
    if any(marker in lowered for marker in noisy_runtime_markers):
        return True
    issue = classify_agent_warning(message)
    hidden_issue_prefixes = [
        "The generated app hit an API authorization/runtime warning during verification.",
        "The generated app logged runtime warnings while the agent was checking its output.",
    ]
    return issue in hidden_issue_prefixes


async def run_status_call(
    key: str,
    phase: str,
    message: str,
    func,
    progress_items: dict[str, str],
    progress_message: cl.Message,
):
    progress_items[key] = f"- `{phase}` {message}"
    progress_message.content = render_progress(progress_items)
    await progress_message.update()
    task = asyncio.create_task(asyncio.to_thread(func))
    tick = 0
    while not task.done():
        await asyncio.sleep(2)
        if task.done():
            break
        tick += 1
        progress_items[key] = (
            f"- `{phase}` {message} Still waiting on coordinator I/O; "
            f"attempt `{tick}`."
        )
        progress_message.content = render_progress(progress_items)
        await progress_message.update()
    result = await task
    progress_items[key] = f"- `{phase}` {message} Done."
    progress_message.content = render_progress(progress_items)
    await progress_message.update()
    return result


def progress_key(update: ProgressUpdate) -> str:
    if update.role == "coordinator" and update.phase in {
        "intake",
        "coordinator-check",
        "routing",
        "delegation",
        "assigned",
        "coordinator-ready",
        "synthesis",
    }:
        return "run:coordinator"
    if update.phase in {"agent-output", "agent-warning", "agent-error"}:
        machine = update.assigned_machine or "local"
        role = update.role or "agent"
        backend = update.preferred_backend or "backend"
        fingerprint_source = humanize_progress_message(update)
        fingerprint = abs(hash((update.phase, machine, role, backend, progress_message_fingerprint(fingerprint_source))))
        return f"stream:{machine}:{role}:{backend}:{fingerprint}"
    if update.task_id:
        return f"task:{update.task_id}"
    if update.role:
        return f"role:{update.role}"
    return f"phase:{update.phase}"


def progress_message_fingerprint(message: str) -> str:
    clean = " ".join(str(message or "").split())
    clean = re.sub(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b", "<time>", clean)
    clean = re.sub(r"\bElapsed: \d+s\.", "Elapsed: <n>s.", clean)
    return clean


def render_progress(items: dict[str, str]) -> str:
    lines = list(items.values())[-8:]
    return "## Coordination Status\n\n" + "\n".join(lines)


def should_hide_turn_completion(turn) -> bool:
    agent = str(getattr(turn, "agent", "") or "").strip().lower()
    role = str(getattr(turn, "role", "") or "").strip().lower()
    return agent == "coordinator" or role == "planning lead"


def is_lightweight_chat(goal: str) -> bool:
    clean = goal.strip().lower()
    return bool(re.fullmatch(r"(hi|hello|hey|yo|sup|test|smoke|hello there|hello .{1,40})", clean))


def brief_agent_chat_content(content: str) -> str:
    clean_content = clean_agent_chat_content(content)
    if worker_result_needs_recovery(clean_content):
        return clean_content
    skipped = {"workstreams", "dependencies", "success", "validation", "routing"}
    for line in clean_content.splitlines():
        clean = line.strip(" -")
        if clean and clean.lower().rstrip(":") not in skipped:
            return clean
    return ""


def worker_result_needs_recovery(content: str) -> bool:
    lowered = content.lower()
    return any(
        marker in lowered
        for marker in [
            "reported a tool-access failure",
            "could not access the project workspace",
            "selected local-agent command is not callable",
            "no completed result came back",
            "cli command was not reachable",
        ]
    )


def agent_setup_failure(content: str) -> bool:
    lowered = content.lower()
    return (
        "was selected, but its cli command was not reachable" in lowered
        or "is selected, but not connected for headless runs" in lowered
        or "selected local-agent command is not callable" in lowered
    )


def routing_response_with_setup_issue(run: OrchestrationRun, setup_message: str) -> str:
    assignments = "\n".join(
        f"- `{task.role}` -> `{task.assigned_machine}` via `{task.preferred_backend}` (`{task.status}`)"
        for task in run.delegated_tasks
    )
    setup_line = first_content_line(setup_message)
    return (
        "I routed the work, but one assigned local agent is not connected for headless execution yet.\n\n"
        f"{assignments}\n\n"
        f"Setup needed: {setup_line}\n\n"
        "The dashboard will keep showing which machine owns each role. Connect the missing local CLI/API on "
        "that machine, then rerun the task."
    )


def clean_agent_chat_content(content: str) -> str:
    lines = []
    skip_next_blank = False
    for line in content.splitlines():
        clean = line.strip()
        if is_noisy_agent_output_line(clean):
            skip_next_blank = False
            continue
        if re.fullmatch(r"`[^`]+`\s+local response", clean, flags=re.IGNORECASE):
            skip_next_blank = True
            continue
        if re.fullmatch(r"`[^`]+`\s+api(?: fallback)? response", clean, flags=re.IGNORECASE):
            skip_next_blank = True
            continue
        if re.fullmatch(r"`[^`]+`\s+result from\s+`[^`]+`", clean, flags=re.IGNORECASE):
            skip_next_blank = True
            continue
        if skip_next_blank and not clean:
            skip_next_blank = False
            continue
        skip_next_blank = False
        lines.append(line)
    return "\n".join(lines).strip()


def is_noisy_agent_output_line(line: str) -> bool:
    lowered = str(line or "").lower()
    return (
        "warning: could not open directory '.pytest-tmp" in lowered
        or "warning: could not open directory \".pytest-tmp" in lowered
        or "codex_core::shell_snapshot" in lowered
        or "codex_core_skills::loader: ignoring interface.icon_" in lowered
        or "failed to clean up stale arg0 temp dirs" in lowered
        or "proceeding, even though we could not update path" in lowered
        or "\\.codex\\tmp\\arg0\\" in lowered
        or ("tcp " in lowered and "time_wait" in lowered)
    )


async def handle_command(text: str) -> None:
    parts = text.split()
    command = parts[0].lower()

    try:
        if command == "/dashboard":
            await show_dashboard_sidebar()
        elif command == "/chats":
            await show_chats()
        elif command == "/new-chat":
            await start_new_chat()
        elif command == "/archive-chat":
            await archive_active_chat()
        elif command == "/help":
            await cl.Message(content=command_help(), actions=machine_actions()).send()
        elif command == "/detect-agents":
            await auto_detect_agents()
        elif command == "/restart-app":
            await restart_app()
        elif command == "/spaces":
            await show_spaces()
        elif command in {"/project", "/set-project"}:
            await set_project_space()
        elif command == "/use" and len(parts) == 2:
            project = spaces.get(parts[1])
            cl.user_session.set("project_space", project)
            save_preferences({"project_name": project.name})
            await cl.Message(content=f"Active project space set to `{project.name}`.").send()
        elif command == "/create-space" and len(parts) >= 3:
            name = parts[1]
            path = " ".join(parts[2:])
            project = spaces.upsert(name, path)
            cl.user_session.set("project_space", project)
            save_preferences({"project_name": project.name})
            await cl.Message(content=f"Created and selected `{project.name}` at `{project.path}`.").send()
        elif command == "/bind-repo" and len(parts) >= 3:
            name = parts[1]
            repo_path = " ".join(parts[2:])
            project = spaces.bind_repository(name, repo_path)
            cl.user_session.set("project_space", project)
            save_preferences({"project_name": project.name})
            await cl.Message(
                content=(
                    f"Bound `{project.name}` to the existing repo at `{project.path}`.\n\n"
                    f"{project_repo_sync_summary(project)}"
                )
            ).send()
        elif command == "/worktree" and len(parts) >= 4:
            name, repo_path, branch = parts[1], parts[2], parts[3]
            project = spaces.create_worktree(name, repo_path, branch)
            cl.user_session.set("project_space", project)
            save_preferences({"project_name": project.name})
            await cl.Message(content=f"Created worktree `{project.name}` at `{project.path}`.").send()
        elif command == "/clone" and len(parts) >= 3:
            name, git_url = parts[1], parts[2]
            branch = parts[3] if len(parts) >= 4 else None
            project = spaces.clone_repository(name, git_url, branch)
            cl.user_session.set("project_space", project)
            save_preferences({"project_name": project.name})
            await cl.Message(content=f"Cloned and selected `{project.name}` at `{project.path}`.").send()
        elif command == "/share-project":
            await share_project()
        elif command == "/join-project":
            await join_project()
        elif command == "/workspace-modes":
            await show_workspace_modes()
        elif command == "/artifacts":
            await show_artifacts()
        elif command == "/layout":
            await show_layout()
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
            await cl.Message(content="Cleared the saved history for the active local chat.").send()
            await show_dashboard_sidebar()
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


async def share_project() -> None:
    project = cl.user_session.get("project_space")
    if project is None:
        await cl.Message(content="Pick or create a project space first.").send()
        return
    if not project.git_remote:
        await cl.Message(
            content=(
                "## Shared Repo Not Ready Yet\n\n"
                f"`{project.name}` is local-only right now, so another machine cannot auto-join it yet.\n\n"
                "Use one of these first:\n\n"
                f"- `/clone {project.name} <git-url> [branch]`\n"
                f"- `/bind-repo {project.name} <local-repo-path>`\n\n"
                "Once the project is backed by a Git remote, this panel will surface a copyable share pack."
            )
        ).send()
        return
    await cl.Message(
        content=(
            "## Project Share Pack\n\n"
            "Paste this into **Join shared repo** on another machine:\n\n"
            "```text\n"
            f"{project_share_pack(project)}\n"
            "```\n\n"
            f"{project_repo_sync_summary(project)}"
        )
    ).send()
    await show_dashboard_sidebar()


async def join_project(share_text: str | None = None) -> None:
    if share_text is None:
        share_text = await ask_text(
            "Paste a project share pack or just a Git remote URL.",
            "",
        )
    if share_text is None:
        return

    details = parse_project_share_pack(share_text)
    git_remote = details.get("git_remote", "").strip()
    branch = details.get("branch", "").strip() or None
    project_name = slugify_project_name(
        details.get("project_name", "").strip() or project_name_from_remote(git_remote or "shared-project")
    )
    if not git_remote:
        await cl.Message(
            content=(
                "I could not find a Git remote in that share pack. Paste the pack from the host machine, "
                "or paste a repository URL directly."
            )
        ).send()
        return

    project = spaces.attach_or_clone_repository(project_name, git_remote, branch)
    cl.user_session.set("project_space", project)
    save_preferences({"project_name": project.name})
    chat_state = chat_thread_state()
    active_id = str(chat_state.get("active_id", ""))
    if active_id:
        set_chat_thread_project(active_id, project.name)
    await cl.Message(
        content=(
            "## Shared Repo Joined\n\n"
            f"Active project: `{project.name}`  \n"
            f"Local path: `{project.path}`\n\n"
            f"{project_repo_sync_summary(project)}"
        )
    ).send()
    await show_dashboard_sidebar()


def activate_project_space(project_name: str, bind_to_active_chat: bool = True) -> ProjectSpace:
    project = ensure_project_space(project_name)
    project = spaces.upsert(
        project.name,
        project.path,
        git_remote=project.git_remote,
        mode=project.mode,
        source=project.source,
        project_id=project.project_id,
        source_kind=project.source_kind,
        visibility=project.visibility,
    )
    cl.user_session.set("project_space", project)
    save_preferences({"project_name": project.name})
    if bind_to_active_chat:
        chat_state = chat_thread_state()
        active_id = str(chat_state.get("active_id", ""))
        if active_id:
            set_chat_thread_project(active_id, project.name)
    return project


def reset_chat_task_context() -> None:
    cl.user_session.set("last_goal", "")
    cl.user_session.set("last_run", None)
    refresh_advertised_backends(selected_chat_backend(), "")


def project_repo_sync_summary(project: ProjectSpace | None) -> str:
    if project is None:
        return "Choose a project space before wiring up shared repo sync."
    if project.git_remote:
        branch = project.branch or "default branch"
        return (
            f"Canonical repo: `{project.git_remote}` on `{branch}` with `{project.visibility}` visibility. "
            "Other machines can clone or join this same repo, work on their assigned slices, then push branches or commits back for merge."
        )
    if project.mode in {"repo", "worktree"}:
        return (
            "This project is inside a local Git checkout but does not advertise an origin remote yet. "
            "Add an origin remote, or clone from GitHub, so other machines can join it automatically."
        )
    return (
        "This project space is local-only. For cross-machine code sync, back it with GitHub or another shared Git remote."
    )


def project_share_ready(project: ProjectSpace | None) -> bool:
    return bool(project and project.git_remote)


def project_share_hint(project: ProjectSpace | None, online_count: int = 1) -> str:
    if project_share_ready(project):
        return (
            f"Copy this pack on the host machine, then paste it into Join shared repo on the other machine. "
            f"Source: `{project.source_kind}`. Visibility: `{project.visibility}`."
        )
    if project is None:
        return "Create or pick a project space first."
    if online_count > 1:
        return (
            "More than one machine is online, but this project is still local-only. "
            "Attach a GitHub or Git remote now so code can converge cleanly across machines."
        )
    return (
        "To sync code across separate machines with minimal setup, clone a GitHub repo here or bind an existing local checkout first."
    )


def source_kind_from_remote(remote_url: str, fallback: str = "git") -> str:
    normalized = (remote_url or "").strip().lower()
    if "github.com" in normalized:
        return "github"
    return normalize_project_source_kind(fallback or "git")


def github_cli_path() -> str | None:
    return shutil.which("gh")


@functools.lru_cache(maxsize=1)
def github_authenticated_user() -> str:
    gh = github_cli_path()
    if not gh:
        return ""
    try:
        result = subprocess.run(
            [gh, "api", "user", "-q", ".login"],
            capture_output=True,
            text=True,
            check=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def create_github_repo_for_project(
    project_name: str,
    project_path: Path,
    owner: str,
    visibility: str,
) -> str:
    gh = github_cli_path()
    if not gh:
        raise ProjectSpaceError("GitHub CLI (`gh`) is not available on this machine.")

    repo_slug = slugify_project_name(project_name)
    full_name = f"{owner}/{repo_slug}"
    visibility_flag = "--public" if visibility == "public" else "--private"
    spaces.ensure_git_repository(project_path)

    view_result = subprocess.run(
        [gh, "repo", "view", full_name, "--json", "nameWithOwner"],
        capture_output=True,
        text=True,
        timeout=12,
    )
    if view_result.returncode != 0:
        try:
            subprocess.run(
                [gh, "repo", "create", full_name, visibility_flag, "--source", str(project_path), "--remote", "origin"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except OSError as exc:
            raise ProjectSpaceError(f"Could not start GitHub CLI: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            details = stderr or stdout or "GitHub CLI returned an error while creating the repository."
            raise ProjectSpaceError(details) from exc
    return f"https://github.com/{full_name}.git"


def unique_new_chat_project_name() -> str:
    existing = {space.name for space in spaces.list_spaces()}
    for _ in range(12):
        name = f"chat-{secrets.token_hex(2)}"
        if name not in existing:
            return name
    return f"chat-{secrets.token_hex(4)}"


async def set_project_space() -> None:
    current = cl.user_session.get("project_space")
    default_name = current.name if current else slugify_project_name(load_preferences().get("project_name", "default"))
    project_name = await ask_text(
        "Project space name. This creates or selects a writable folder under the local workspaces directory.",
        default_name,
    )
    if project_name is None:
        return

    project = activate_project_space(project_name)
    await setup_chat_settings()
    await cl.Message(
        content=(
            "## Project Space Set\n\n"
            f"Active project: `{project.name}`\n\n"
            f"Writable folder: `{project.path}`\n\n"
            "Agents will use this workspace for generated files, previews, and handoffs."
        )
    ).send()
    await show_dashboard_sidebar()


async def save_project_space_from_action(project_name: str, silent: bool = False) -> None:
    if not project_name.strip():
        if not silent:
            await cl.Message(content="Project space name cannot be empty.").send()
        await show_dashboard_sidebar()
        return
    project = activate_project_space(project_name)
    await setup_chat_settings()
    if not silent:
        await cl.Message(
            content=(
                "## Project Space Saved\n\n"
                f"Active project: `{project.name}`\n\n"
                f"Writable folder: `{project.path}`"
            )
        ).send()
    await show_dashboard_sidebar()


async def save_project_profile_from_action(
    project_name: str,
    source_kind: str,
    visibility: str,
    git_remote: str,
    silent: bool = False,
) -> None:
    current = cl.user_session.get("project_space")
    if current is None:
        current = initial_project_space()
    clean_name = slugify_project_name(project_name or current.name)
    clean_source_kind = normalize_project_source_kind(source_kind)
    clean_visibility = normalize_project_visibility(visibility)
    clean_remote = git_remote.strip()
    if clean_source_kind in {"github", "git"} and not clean_remote:
        clean_source_kind = "none"
        if clean_visibility != "public":
            clean_visibility = "local-only"
    project = spaces.update_profile(
        current.name,
        project_name=clean_name,
        source_kind=clean_source_kind,
        visibility=clean_visibility,
        git_remote=clean_remote or None,
    )
    cl.user_session.set("project_space", project)
    save_preferences({"project_name": project.name})
    chat_state = chat_thread_state()
    active_id = str(chat_state.get("active_id", ""))
    if active_id:
        set_chat_thread_project(active_id, project.name)
    if not silent:
        await cl.Message(
            content=(
                "## Project Profile Saved\n\n"
                f"Project: `{project.name}`  \n"
                f"Project ID: `{project.project_id}`  \n"
                f"Source: `{project.source_kind}`  \n"
                f"Visibility: `{project.visibility}`  \n"
                f"Remote: `{project.git_remote or 'not set'}`"
            )
        ).send()
    await show_dashboard_sidebar()


async def provision_project_repo_from_action(
    project_name: str,
    source_kind: str,
    visibility: str,
    git_remote: str,
    silent: bool = False,
) -> None:
    current = cl.user_session.get("project_space")
    if current is None:
        current = initial_project_space()

    clean_name = slugify_project_name(project_name or current.name)
    clean_source_kind = normalize_project_source_kind(source_kind)
    clean_visibility = normalize_project_visibility(visibility)
    clean_remote = git_remote.strip()

    project = activate_project_space(clean_name)

    if clean_remote:
        spaces.configure_remote(project.path, clean_remote)
        final_source_kind = source_kind_from_remote(clean_remote, clean_source_kind)
        final_visibility = clean_visibility if clean_visibility != "local-only" else "private"
        project = spaces.update_profile(
            project.name,
            source_kind=final_source_kind,
            visibility=final_visibility,
            git_remote=clean_remote,
        )
        cl.user_session.set("project_space", project)
        if not silent:
            await cl.Message(
                content=(
                    "## Remote Attached\n\n"
                    f"Project: `{project.name}`  \n"
                    f"Remote: `{project.git_remote}`  \n"
                    f"Source: `{project.source_kind}`  \n"
                    f"Visibility: `{project.visibility}`"
                )
            ).send()
        await show_dashboard_sidebar()
        return

    if clean_source_kind != "github":
        await cl.Message(
            content=(
                "To publish online from the UI, either paste an existing Git remote URL or choose `github` as the source."
            )
        ).send()
        await show_dashboard_sidebar()
        return

    owner = github_authenticated_user()
    if not owner:
        await cl.Message(
            content=(
                "GitHub repo creation needs the local GitHub CLI (`gh`) signed in on this machine.\n\n"
                "Run `gh auth login`, then retry **Create GitHub repo** from the project card."
            )
        ).send()
        await show_dashboard_sidebar()
        return

    final_visibility = clean_visibility if clean_visibility in {"private", "public"} else "private"
    remote_url = create_github_repo_for_project(project.name, project.path, owner, final_visibility)
    spaces.configure_remote(project.path, remote_url)
    project = spaces.update_profile(
        project.name,
        source_kind="github",
        visibility=final_visibility,
        git_remote=remote_url,
    )
    cl.user_session.set("project_space", project)
    if not silent:
        await cl.Message(
            content=(
                "## GitHub Repo Ready\n\n"
                f"Project: `{project.name}`  \n"
                f"Owner: `{owner}`  \n"
                f"Remote: `{project.git_remote}`  \n"
                f"Visibility: `{project.visibility}`\n\n"
                "Other machines can now use the shared repo pack from the dashboard to join this same project."
            )
        ).send()
    await show_dashboard_sidebar()


async def show_chats() -> None:
    state = chat_thread_state()
    threads = state.get("threads", [])
    if not threads:
        await cl.Message(content="No saved local chats yet.").send()
        return
    lines = []
    active_id = state.get("active_id", "")
    for thread in threads:
        marker = "active" if thread["id"] == active_id else "saved"
        preview = f" - {thread['preview']}" if thread.get("preview") else ""
        lines.append(
            f"- `{marker}` `{thread['message_count']} msg` **{thread['title']}**{preview}"
        )
    archived_count = int(state.get("archived_count") or 0)
    archived = f"\n\nArchived chats: `{archived_count}`" if archived_count else ""
    await cl.Message(content="## Local Chats\n\n" + "\n".join(lines) + archived).send()
    await show_dashboard_sidebar()


async def start_new_chat() -> None:
    project_name = unique_new_chat_project_name()
    thread = create_chat_thread(project_name=project_name)
    project = activate_project_space(thread.project_name or project_name)
    cl.user_session.set("active_chat_id", thread.id)
    reset_chat_task_context()
    await setup_chat_settings()
    await cl.Message(
        content=(
            f"Started `{thread.title}`. New messages will be saved to this chat, "
            f"using fresh project space `{project.name}`."
        )
    ).send()
    await show_dashboard_sidebar()


async def start_new_chat_silently() -> None:
    project_name = unique_new_chat_project_name()
    thread = create_chat_thread(project_name=project_name)
    activate_project_space(thread.project_name or project_name)
    cl.user_session.set("active_chat_id", thread.id)
    reset_chat_task_context()
    await setup_chat_settings()
    await show_dashboard_sidebar()


async def switch_chat(chat_id: str, silent: bool = False) -> None:
    thread = set_active_chat_thread(chat_id)
    if thread is None:
        if not silent:
            await cl.Message(content="I could not find that saved chat, or it has been archived.").send()
        await show_dashboard_sidebar()
        return
    cl.user_session.set("active_chat_id", thread.id)
    if thread.project_name:
        activate_project_space(thread.project_name)
    reset_chat_task_context()
    await setup_chat_settings()
    if not silent:
        await cl.Message(content=f"Switched to `{thread.title}`. Recent saved messages are below.").send()
        await restore_chat_history(thread.id, force=True)
    await show_dashboard_sidebar()


async def archive_active_chat(chat_id: str | None = None, silent: bool = False) -> None:
    archived = archive_chat_thread(chat_id)
    state = chat_thread_state()
    cl.user_session.set("active_chat_id", state["active_id"])
    if state.get("active_project_name"):
        activate_project_space(str(state["active_project_name"]))
    reset_chat_task_context()
    await setup_chat_settings()
    if not silent:
        await cl.Message(
            content=(
                f"Archived `{archived.title}`. Active chat is now `{state['active_title']}`."
            )
        ).send()
    await show_dashboard_sidebar()


async def setup_chat_settings(selected_backend: str | None = None) -> None:
    preferences = load_preferences()
    credentials = load_credentials()
    project = cl.user_session.get("project_space")
    project_name = project.name if project else slugify_project_name(preferences.get("project_name", "default"))
    restore_history = preferences.get("restore_history", "true").lower() != "false"
    codex_credentials = credentials.get(CODEX_BACKEND, {})
    claude_credentials = credentials.get(CLAUDE_CODE_BACKEND, {})
    gemini_credentials = credentials.get(GEMINI_CLI_BACKEND, {})
    openswarm_credentials = credentials.get(OPEN_SWARM_BACKEND, {})
    openai_api_key = clean_secret_input(codex_credentials.get("openai_api_key", "")) or settings.openai_api_key.strip()
    claude_api_key = clean_secret_input(claude_credentials.get("claude_api_key", "")) or settings.claude_api_key.strip()
    gemini_api_key = clean_secret_input(gemini_credentials.get("gemini_api_key", "")) or settings.gemini_api_key.strip()
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
            "project_name": project_name,
            "codex_command": codex_command,
            "claude_command": claude_command,
            "gemini_command": gemini_command,
        }
    )
    widgets = [
        TextInput(
            id="project_name",
            label="Project Space Name",
            initial=project_name,
            placeholder="my-project",
            tooltip="Creates or selects a writable project folder under the local workspaces directory.",
        ),
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
    if backend in {CLAUDE_CODE_BACKEND, GEMINI_CLI_BACKEND} and load_agent_api_keys().get(backend, ""):
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
            "the sidebar. You can also save a Claude API key in Settings for API fallback when the local "
            "CLI is not available. Use **Auto-detect Agents** after setup, or **Restart App** if PATH changed."
        )
    if backend == GEMINI_CLI_BACKEND:
        return (
            "## Gemini CLI Is Selected, But Not Connected For Headless Runs\n\n"
            "This harness talks to Gemini through the local `gemini` command. Sign in through Gemini CLI's "
            "normal terminal flow, make sure `gemini` is on `PATH`, or set the full command path in the "
            "sidebar. You can also save a Gemini API key in Settings for API fallback when the local CLI is "
            "not available. Use **Auto-detect Agents** after setup, or **Restart App** if PATH changed."
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


def load_agent_api_keys() -> dict[str, str]:
    credentials = load_credentials()
    try:
        openai_key = cl.user_session.get("openai_api_key")
        claude_key = cl.user_session.get("claude_api_key")
        gemini_key = cl.user_session.get("gemini_api_key")
    except Exception:
        openai_key = ""
        claude_key = ""
        gemini_key = ""
    codex_credentials = credentials.get(CODEX_BACKEND, {})
    claude_credentials = credentials.get(CLAUDE_CODE_BACKEND, {})
    gemini_credentials = credentials.get(GEMINI_CLI_BACKEND, {})
    return {
        CODEX_BACKEND: clean_secret_input(openai_key)
        or clean_secret_input(codex_credentials.get("openai_api_key", ""))
        or settings.openai_api_key.strip(),
        CLAUDE_CODE_BACKEND: clean_secret_input(claude_key)
        or clean_secret_input(claude_credentials.get("claude_api_key", ""))
        or settings.claude_api_key.strip()
        or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("CLAUDE_API_KEY", "").strip(),
        GEMINI_CLI_BACKEND: clean_secret_input(gemini_key)
        or clean_secret_input(gemini_credentials.get("gemini_api_key", ""))
        or settings.gemini_api_key.strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip(),
    }


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


async def restore_chat_history(chat_id: str | None = None, force: bool = False) -> None:
    if not force and not cl.user_session.get("restore_history", True):
        return
    history = [
        record
        for record in load_chat_history(chat_id=chat_id)
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


async def show_artifacts() -> None:
    project = cl.user_session.get("project_space")
    await cl.Message(content=artifacts_markdown(project), actions=machine_actions()).send()


async def show_layout() -> None:
    project = cl.user_session.get("project_space")
    await cl.Message(content=workspace_layout_markdown(project), actions=machine_actions()).send()
    await show_dashboard_sidebar()


async def show_backends() -> None:
    await cl.Message(content=backend_status_cards(), actions=machine_actions()).send()


async def show_connection() -> None:
    await update_cluster_roster()
    await cl.Message(content=connection_cards(), actions=machine_actions()).send()


async def configure_http_connection(connection_text: str | None = None) -> None:
    if connection_text is None or not connection_text.strip():
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


async def host_coordinator(project_name: str | None = None) -> None:
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

    default_project_name = default_host_project_name()
    if project_name is None or not project_name.strip():
        project_name = default_project_name
    cluster_id = hosted_cluster_id(project_name)
    token = secrets.token_urlsafe(24)
    port = find_available_port(settings.coordinator_port)
    await cl.Message(
        content=(
            "Starting a coordinator on this machine...\n\n"
            f"Hosted project: `{cluster_id}`"
        )
    ).send()
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
            "> ### Bind Existing Repo\n"
            "> Use when the repo is already cloned on this machine and you just want this UI to adopt it.  \n"
            "> Command: `/bind-repo <name> <repo-path>`\n\n"
            "> ### Local Folder Mode\n"
            "> Use when the project already exists as a local folder.  \n"
            "> Command: `/create-space <name> <path>`\n\n"
            "> ### Share / Join Across Machines\n"
            "> Once a project has a Git remote, use `/share-project` on one machine and `/join-project` on another."
        )
    ).send()


@cl.action_callback("refresh_machines")
async def refresh_machines(_: cl.Action) -> None:
    await show_machines()


@cl.action_callback("refresh_dashboard")
async def refresh_dashboard(_: cl.Action) -> None:
    try:
        await show_dashboard_sidebar()
    except Exception:
        LOGGER.exception("dashboard refresh failed")


@cl.action_callback("new_chat")
async def new_chat_action(action: cl.Action) -> None:
    payload = action_payload(action)
    if payload.get("silent"):
        await start_new_chat_silently()
    else:
        await start_new_chat()


@cl.action_callback("switch_chat")
async def switch_chat_action(action: cl.Action) -> None:
    payload = action_payload(action)
    chat_id = str(payload.get("chat_id", "")).strip()
    if not chat_id:
        await cl.Message(content="No chat was selected.").send()
        return
    await switch_chat(chat_id, silent=bool(payload.get("silent")))


@cl.action_callback("archive_chat")
async def archive_chat_action(action: cl.Action) -> None:
    payload = action_payload(action)
    chat_id = str(payload.get("chat_id", "")).strip() or None
    await archive_active_chat(chat_id, silent=bool(payload.get("silent")))


@cl.action_callback("restore_chat")
async def restore_chat_action(action: cl.Action) -> None:
    payload = action_payload(action)
    chat_id = str(payload.get("chat_id", "")).strip() or str(cl.user_session.get("active_chat_id") or "")
    await restore_chat_history(chat_id, force=True)


@cl.action_callback("save_project_space")
async def save_project_space_action(action: cl.Action) -> None:
    payload = action_payload(action)
    project_name = str(payload.get("project_name", "")).strip()
    await save_project_space_from_action(project_name, silent=bool(payload.get("silent")))


@cl.action_callback("save_project_profile")
async def save_project_profile_action(action: cl.Action) -> None:
    payload = action_payload(action)
    await save_project_profile_from_action(
        str(payload.get("project_name", "")).strip(),
        str(payload.get("source_kind", "")).strip(),
        str(payload.get("visibility", "")).strip(),
        str(payload.get("git_remote", "")).strip(),
        silent=bool(payload.get("silent")),
    )


@cl.action_callback("provision_project_repo")
async def provision_project_repo_action(action: cl.Action) -> None:
    payload = action_payload(action)
    await provision_project_repo_from_action(
        str(payload.get("project_name", "")).strip(),
        str(payload.get("source_kind", "")).strip(),
        str(payload.get("visibility", "")).strip(),
        str(payload.get("git_remote", "")).strip(),
        silent=bool(payload.get("silent")),
    )


@cl.action_callback("share_project")
async def share_project_action(_: cl.Action) -> None:
    await share_project()


@cl.action_callback("join_project")
async def join_project_action(action: cl.Action) -> None:
    payload = action_payload(action)
    share_text = str(payload.get("share_text", "")).strip() or None
    await join_project(share_text)


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


@cl.action_callback("set_project_space")
async def set_project_space_action(_: cl.Action) -> None:
    await set_project_space()


@cl.action_callback("show_artifacts")
async def show_artifacts_action(_: cl.Action) -> None:
    await show_artifacts()


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
async def host_coordinator_action(action: cl.Action) -> None:
    payload = action_payload(action)
    project_name = str(payload.get("project_name", "")).strip() or None
    await host_coordinator(project_name)


@cl.action_callback("configure_http")
async def configure_http_action(action: cl.Action) -> None:
    payload = action_payload(action)
    connection_text = str(payload.get("connection_text", "")).strip() or None
    await configure_http_connection(connection_text)


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
    await cl.Message(content="Cleared the saved history for the active local chat.").send()
    await show_dashboard_sidebar()


def action_payload(action: cl.Action) -> dict:
    payload = getattr(action, "payload", None)
    return payload if isinstance(payload, dict) else {}


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
            name="show_artifacts",
            label="Artifacts",
            tooltip="Show generated project files and preview commands.",
            icon="file-code-2",
            payload={},
        ),
        cl.Action(
            name="set_project_space",
            label="Project",
            tooltip="Set the active project space name.",
            icon="folder",
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
                name="HarnessDashboardV2",
                display="side",
                props=dashboard_props(machines, orchestrator_id),
            )
        ]
    )


def dashboard_props(machines: list[MachineNode], orchestrator_id: str) -> dict:
    project = cl.user_session.get("project_space")
    last_goal = str(cl.user_session.get("last_goal") or "")
    tasks_snapshot = dashboard_tasks_snapshot()
    current_run_id = current_dashboard_run_id(tasks_snapshot, last_goal)
    current_tasks = tasks_for_run(tasks_snapshot, current_run_id)
    artifacts = scan_project_artifacts(project)
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
    share_pack = project_share_pack(project) if project_share_ready(project) else ""
    canonical_repo = (
        f"{project.git_remote} ({project.visibility})" if project and project.git_remote else "local-only workspace"
    )
    merge_flow = (
        f"workers clone `{project.git_remote}` and return branches against `{project.branch or 'default branch'}`"
        if project and project.git_remote
        else "back this project with a shared Git remote before expecting cross-machine code sync"
    )
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
            "last_refreshed": local_time_label(),
            "token_set": bool(settings.coordination_token),
            "a2a_enabled": hosting_live or connected_to_http,
            "a2a_version": A2A_PROTOCOL_VERSION,
            "a2a_agent_card_url": f"{a2a_base_url.rstrip('/')}/.well-known/agent-card.json" if a2a_base_url else "",
            "a2a_rpc_url": f"{a2a_base_url.rstrip('/')}/a2a/rpc" if a2a_base_url else "",
        },
        "workspace": {
            "name": project.name if project else "default",
            "project_id": project.project_id if project else "",
            "path": str(project.path) if project else str(settings.workspaces_root / "default"),
            "mode": project.mode if project else "local",
            "source_kind": project.source_kind if project else "none",
            "visibility": project.visibility if project else "local-only",
            "branch": project.branch if project else "",
            "remote": project.git_remote if project else "",
            "share_ready": project_share_ready(project),
            "share_pack": share_pack,
            "share_hint": project_share_hint(project, online_count),
        },
        "policy": {
            "selected_backend": selected_backend,
            "selected_ready": selected_ready,
            "capabilities": local_capabilities,
            "goal_roles": [task.role for task in current_tasks],
            "last_goal": last_goal,
        },
        "repo": {
            "has_goal": bool(last_goal),
            "code_path": str(project.path) if project else str(settings.workspaces_root / "default"),
            "preview_command": preview_command(project),
            "artifacts": [artifact.__dict__ for artifact in artifacts],
            "evaluation": build_evaluation(project, current_tasks),
            "worker_outputs": repo_worker_outputs(current_tasks),
            "canonical": canonical_repo,
            "merge": merge_flow,
        },
        "run": dashboard_run_props(tasks_snapshot, current_run_id),
        "machines": [machine_dashboard_props(machine, tasks_snapshot, current_run_id) for machine in machines],
        "chats": chat_thread_state(),
    }


def local_time_label() -> str:
    """Return a compact timestamp in this machine's local timezone for UI display."""
    return datetime.now().astimezone().strftime("%H:%M:%S")


def current_dashboard_run_id(tasks_snapshot: list[DelegatedTask] | None = None, last_goal: str = "") -> str:
    run = cl.user_session.get("last_run")
    if run and getattr(run, "run_id", ""):
        return str(getattr(run, "run_id", ""))
    if not last_goal.strip():
        return ""
    for task in tasks_snapshot or []:
        if task.run_id and task.goal == last_goal:
            return task.run_id
    return ""


def tasks_for_run(tasks: list[DelegatedTask] | None, run_id: str) -> list[DelegatedTask]:
    if not tasks:
        return []
    if not run_id:
        return []
    return [task for task in tasks if task.run_id == run_id]


def machine_dashboard_props(
    machine: MachineNode,
    tasks: list[DelegatedTask] | None = None,
    run_id: str = "",
) -> dict:
    age = datetime.now(UTC) - machine.last_seen
    machine_tasks = [
        task
        for task in tasks_for_run(tasks or [], run_id)
        if task.assigned_machine == machine.machine_id
    ][:4]
    assignments = [
        task_dashboard_props(task)
        for task in reversed(machine_tasks)
    ]
    assigned_roles = unique_preserving_order([task["role"] for task in assignments])
    active_roles = unique_preserving_order(
        [task["role"] for task in assignments if task["status"] in {"delegated", "running"}]
    )
    visible_capabilities = unique_preserving_order([*assigned_roles, *machine.capabilities])
    return {
        "machine_id": machine.machine_id,
        "hostname": machine.hostname,
        "role": machine.role,
        "status": machine.status,
        "status_label": _machine_status(machine),
        "seen_seconds": max(0, int(age.total_seconds())),
        "capabilities": visible_capabilities,
        "agent_backends": machine.agent_backends,
        "active_roles": active_roles,
        "assigned_roles": assigned_roles,
        "assignments": assignments,
    }


def dashboard_run_props(
    tasks_snapshot: list[DelegatedTask] | None = None,
    current_run_id: str = "",
) -> dict:
    run = cl.user_session.get("last_run")
    last_goal = str(cl.user_session.get("last_goal") or "")
    if not run:
        if not last_goal.strip() and not current_run_id:
            return {
                "run_id": "",
                "goal": "",
                "goal_summary": "",
                "orchestrator_machine": "",
                "turns": [],
                "tasks": [],
                "task_stats": task_completion_stats([]),
            }
        tasks = recent_dashboard_tasks(current_run_id, tasks_snapshot)
        goal = last_goal
        current_tasks = tasks_for_run(tasks_snapshot, current_run_id)
        return {
            "run_id": current_run_id,
            "goal": goal,
            "goal_summary": summarize_goal(goal, current_tasks),
            "orchestrator_machine": "",
            "turns": [],
            "tasks": tasks,
            "task_stats": task_completion_stats(current_tasks),
        }
    run_id = getattr(run, "run_id", "") or current_run_id
    tasks = recent_dashboard_tasks(run_id, tasks_snapshot)
    goal = getattr(run, "goal", "")
    live_run_tasks = tasks_for_run(tasks_snapshot, run_id)
    run_tasks = live_run_tasks or getattr(run, "delegated_tasks", [])
    return {
        "run_id": run_id,
        "goal": goal,
        "goal_summary": getattr(run, "goal_summary", "") or summarize_goal(goal, run_tasks),
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
        "task_stats": task_completion_stats(run_tasks),
    }


def recent_dashboard_tasks(run_id: str, tasks_snapshot: list[DelegatedTask] | None = None) -> list[dict]:
    if not run_id:
        return []
    try:
        tasks = tasks_snapshot if tasks_snapshot is not None else coordination.list_tasks()
    except CoordinationError:
        return []
    tasks = [task for task in tasks if task.run_id == run_id]
    return [task_dashboard_props(task) for task in list(reversed(tasks[:8]))]


def dashboard_tasks_snapshot(limit: int = 50) -> list[DelegatedTask]:
    try:
        return coordination.list_tasks(limit)
    except CoordinationError:
        return []


def task_dashboard_props(task: DelegatedTask) -> dict:
    return {
        "role": task.role,
        "machine": task.assigned_machine,
        "original_machine": task.original_machine,
        "backend": task.preferred_backend,
        "status": task.status,
        "title": task.title,
        "brief": task.brief,
        "progress_note": task.progress_note,
        "claimed_by": task.claimed_by,
        "recovery_count": task.recovery_count,
        "last_recovered_from": task.last_recovered_from,
        "completed_by": task.completed_by,
        "completion_source": task.completion_source,
        "goal": task.goal,
        "task_id": task.task_id,
    }


def repo_worker_outputs(tasks: list[DelegatedTask]) -> str:
    if not tasks:
        return "waiting for task"
    active = [task for task in tasks if task.status in {"delegated", "running"}]
    completed = [task for task in tasks if task.status == "completed"]
    failed = [task for task in tasks if task.status == "failed"]
    parts = []
    if active:
        parts.append(f"{len(active)} active")
    if completed:
        parts.append(f"{len(completed)} completed")
    if failed:
        parts.append(f"{len(failed)} failed")
    return ", ".join(parts) or "waiting for worker outputs"


def unique_preserving_order(values: list[str]) -> list[str]:
    result = []
    for value in values:
        clean = str(value).strip()
        if clean and clean not in result:
            result.append(clean)
    return result


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


def maybe_rename_empty_active_chat(goal: str) -> None:
    state = chat_thread_state()
    active_id = str(state.get("active_id", ""))
    active = next((thread for thread in state.get("threads", []) if thread.get("id") == active_id), None)
    if active and int(active.get("message_count") or 0) == 0:
        rename_chat_thread(active_id, summarize_goal(goal, max_length=52))


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
        "- `/chats`\n"
        "- `/new-chat`\n"
        "- `/archive-chat`\n"
        "- `/help`\n"
        "- `/detect-agents`\n"
        "- `/restart-app`\n"
        "- `/spaces`\n"
        "- `/project` or `/set-project`\n"
        "- `/use <name>`\n"
        "- `/create-space <name> <path>`\n"
        "- `/bind-repo <name> <repo-path>`\n"
        "- `/worktree <name> <repo-path> <branch>`\n"
        "- `/clone <name> <git-url> [branch]`\n"
        "- `/share-project`\n"
        "- `/join-project`\n"
        "- `/workspace-modes`\n"
        "- `/artifacts`\n"
        "- `/layout`\n"
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


def default_host_project_name() -> str:
    project = cl.user_session.get("project_space")
    if project is not None and getattr(project, "name", ""):
        return str(project.name)
    if settings.cluster_id and settings.cluster_id != "local":
        return re.sub(r"-[0-9a-fA-F]{3,4}$", "", settings.cluster_id)
    return "friends-project"


def hosted_cluster_id(project_name: str, suffix: str | None = None) -> str:
    base = slugify_project_name(project_name)
    suffix = suffix or secrets.token_hex(2)
    return f"{base}-{suffix.lower()[:4]}"


def slugify_project_name(project_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", project_name.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "project"


def normalize_project_source_kind(value: str) -> str:
    clean = str(value or "").strip().lower()
    if clean in {"github", "git", "none"}:
        return clean
    return "none"


def normalize_project_visibility(value: str) -> str:
    clean = str(value or "").strip().lower()
    if clean in {"public", "private", "local-only"}:
        return clean
    return "local-only"


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
    timeout_seconds: float = 12.0,
    status_message: cl.Message | None = None,
) -> tuple[str, str] | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    attempts = 0
    last_error = ""
    while asyncio.get_running_loop().time() < deadline:
        for url in urls:
            attempts += 1
            if status_message:
                remaining = max(0, int(deadline - asyncio.get_running_loop().time()))
                status_message.content = (
                    "Checking coordinator reachability...\n\n"
                    f"Trying: `{url}/health`  \n"
                    f"Attempt: `{attempts}`  \n"
                    f"Time left: `{remaining}s`"
                )
                await status_message.update()
            try:
                response = await asyncio.to_thread(httpx.get, f"{url}/health", timeout=1.5, trust_env=False)
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
        await asyncio.sleep(1)
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
        "I retried for about 12 seconds and still could not reach the coordinator.\n\n"
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
