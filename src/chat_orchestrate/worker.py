from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .backends import CLAUDE_CODE_BACKEND, CODEX_BACKEND, GEMINI_CLI_BACKEND, detect_agent_backends, run_task
from .capabilities import infer_machine_capabilities
from .config import Settings, get_settings
from .coordination import CoordinationError, CoordinationManager
from .runtime_config import RUNTIME_CONFIG_PATH, apply_runtime_env, clear_runtime_process_env


LOGGER = logging.getLogger("chat_orchestrate.worker")


def build_coordination(settings: Settings | None = None) -> CoordinationManager:
    settings = settings or get_settings()
    agent_backends = detect_agent_backends(settings.configured_backends, settings.command_overrides)
    agent_roles = infer_machine_capabilities(agent_backends, defaults=settings.default_agents)
    return CoordinationManager(
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


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    coordination = build_coordination(settings)
    pending_results_path = _pending_results_path(settings, coordination.machine_id)
    announced = False
    last_coordination_error = ""

    while True:
        try:
            node = coordination.heartbeat()
            _flush_pending_results(coordination, pending_results_path)
            task = coordination.claim_next_task()
        except CoordinationError as exc:
            message = str(exc)
            if message != last_coordination_error:
                LOGGER.warning("coordinator unavailable; retrying: %s", message)
                last_coordination_error = message
            settings = reload_settings_from_runtime(settings)
            coordination = build_coordination(settings)
            pending_results_path = _pending_results_path(settings, coordination.machine_id)
            await asyncio.sleep(settings.worker_poll_seconds)
            continue

        if not announced:
            LOGGER.info(
                "worker started machine=%s backends=%s dry_run=%s",
                node.machine_id,
                ",".join(node.agent_backends),
                settings.worker_dry_run,
            )
            announced = True
        last_coordination_error = ""

        if task is None:
            await asyncio.sleep(settings.worker_poll_seconds)
            continue

        LOGGER.info(
            "running task=%s role=%s backend=%s",
            task.task_id,
            task.role,
            task.preferred_backend,
        )
        try:
            coordination.note_task_progress(
                task.task_id,
                f"Claimed by `{coordination.machine_id}`. Starting `{task.role}` work: {task.brief or task.title}",
                status="running",
            )
        except Exception:
            pass
        try:
            result = await _run_task_with_lease(task, coordination, settings)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            try:
                coordination.complete_task(
                    task.task_id,
                    str(exc),
                    status="failed",
                    completed_by=coordination.machine_id,
                    completion_source="direct",
                )
            except Exception as coordination_exc:
                LOGGER.warning("could not mark task failed: %s", coordination_exc)
                _append_pending_result(
                    pending_results_path,
                    task.task_id,
                    str(exc),
                    status="failed",
                    machine_id=coordination.machine_id,
                )
            LOGGER.exception("task failed task=%s", task.task_id)
        else:
            try:
                coordination.complete_task(
                    task.task_id,
                    result,
                    completed_by=coordination.machine_id,
                    completion_source="direct",
                )
            except Exception as exc:
                LOGGER.warning("completed task locally but could not report result: %s", exc)
                _append_pending_result(
                    pending_results_path,
                    task.task_id,
                    result,
                    status="completed",
                    machine_id=coordination.machine_id,
                )
            else:
                LOGGER.info("completed task=%s", task.task_id)


async def _run_task_with_lease(task, coordination: CoordinationManager, settings: Settings) -> str:
    runner = asyncio.create_task(
        asyncio.to_thread(
            run_task,
            task,
            dry_run=settings.worker_dry_run,
            command_overrides=settings.command_overrides,
            openai_api_key=settings.openai_api_key,
            codex_api_model=settings.codex_api_model,
            api_keys={
                CODEX_BACKEND: settings.openai_api_key,
                CLAUDE_CODE_BACKEND: settings.claude_api_key,
                GEMINI_CLI_BACKEND: settings.gemini_api_key,
            },
            claude_api_model=settings.claude_api_model,
            gemini_api_model=settings.gemini_api_model,
            workspaces_root=settings.workspaces_root,
        )
    )
    heartbeat_interval = max(2.0, min(settings.worker_poll_seconds, 8.0))
    tick = 0
    while not runner.done():
        try:
            coordination.renew_task_lease(task.task_id)
            tick += 1
            note = _worker_progress_note(task, tick)
            coordination.note_task_progress(task.task_id, note, status="running")
        except CoordinationError as exc:
            LOGGER.warning("could not renew task lease for %s: %s", task.task_id, exc)
        await asyncio.sleep(heartbeat_interval)
    return await runner


def _worker_progress_note(task, tick: int) -> str:
    steps = [
        f"Opening the `{task.project}` workspace for `{task.role}` work.",
        f"Running `{task.preferred_backend}` on the assigned brief: {task.brief or task.title}",
        "Collecting files changed, verification notes, and coordinator handoff details.",
    ]
    return steps[(max(1, tick) - 1) % len(steps)]


def _pending_results_path(settings: Settings, machine_id: str) -> Path:
    name = machine_id.strip() or settings.machine_id.strip() or "local-machine"
    return settings.coordination_state_path.resolve().parent / f".pending-results-{name}.jsonl"


def _append_pending_result(path: Path, task_id: str, result: str, status: str, machine_id: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"task_id": task_id, "result": result, "status": status, "machine_id": machine_id}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _flush_pending_results(coordination: CoordinationManager, path: Path) -> None:
    items = _read_pending_results(path)
    if not items:
        return
    remaining = []
    for item in items:
        task_id = str(item.get("task_id", "")).strip()
        status = str(item.get("status", "completed")).strip() or "completed"
        result = str(item.get("result", ""))
        machine_id = str(item.get("machine_id", "")).strip()
        if not task_id:
            continue
        try:
            coordination.complete_task(
                task_id,
                result,
                status=status,
                completed_by=machine_id or coordination.machine_id,
                completion_source="replayed",
            )
        except Exception:
            remaining.append(item)
    _write_pending_results(path, remaining)
    if remaining:
        LOGGER.info("pending task reports still queued=%s", len(remaining))


def _read_pending_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        clean = line.strip()
        if not clean:
            continue
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _write_pending_results(path: Path, items: list[dict]) -> None:
    if not items:
        if path.exists():
            path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(item) for item in items) + "\n"
    path.write_text(payload, encoding="utf-8")


def reload_settings_from_runtime(current: Settings) -> Settings:
    if RUNTIME_CONFIG_PATH.exists():
        apply_runtime_env(os.environ, override=True)
    else:
        clear_runtime_process_env(os.environ)
    get_settings.cache_clear()
    updated = get_settings()
    if (
        updated.coordination_backend != current.coordination_backend
        or updated.coordination_http_url != current.coordination_http_url
        or updated.coordination_http_urls != current.coordination_http_urls
        or updated.coordination_token != current.coordination_token
        or updated.cluster_id != current.cluster_id
    ):
        LOGGER.info("worker reloaded coordinator runtime config")
    return updated
