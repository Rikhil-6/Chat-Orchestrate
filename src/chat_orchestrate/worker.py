from __future__ import annotations

import asyncio
import logging
import os

from .backends import detect_agent_backends, run_task
from .config import Settings, get_settings
from .coordination import CoordinationError, CoordinationManager
from .runtime_config import RUNTIME_CONFIG_PATH, apply_runtime_env, clear_runtime_process_env


LOGGER = logging.getLogger("chat_orchestrate.worker")


def build_coordination(settings: Settings | None = None) -> CoordinationManager:
    settings = settings or get_settings()
    agent_roles = [*settings.default_agents, "backend", "frontend"]
    agent_roles = list(dict.fromkeys(agent_roles))
    return CoordinationManager(
        settings.coordination_state_path,
        settings.machine_id,
        agent_roles,
        detect_agent_backends(settings.configured_backends, settings.command_overrides),
        settings.orchestrator_ttl_seconds,
        settings.cluster_id,
        settings.coordination_token,
        settings.coordination_backend,
        settings.coordination_http_url,
        settings.coordination_http_urls,
    )


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    coordination = build_coordination(settings)
    announced = False
    last_coordination_error = ""

    while True:
        try:
            node = coordination.heartbeat()
            task = coordination.claim_next_task()
        except CoordinationError as exc:
            message = str(exc)
            if message != last_coordination_error:
                LOGGER.warning("coordinator unavailable; retrying: %s", message)
                last_coordination_error = message
            settings = reload_settings_from_runtime(settings)
            coordination = build_coordination(settings)
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
            result = run_task(
                task,
                dry_run=settings.worker_dry_run,
                command_overrides=settings.command_overrides,
                openai_api_key=settings.openai_api_key,
                codex_api_model=settings.codex_api_model,
            )
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            try:
                coordination.complete_task(task.task_id, str(exc), status="failed")
            except CoordinationError as coordination_exc:
                LOGGER.warning("could not mark task failed: %s", coordination_exc)
            LOGGER.exception("task failed task=%s", task.task_id)
        else:
            try:
                coordination.complete_task(task.task_id, result)
            except CoordinationError as exc:
                LOGGER.warning("completed task locally but could not report result: %s", exc)
            else:
                LOGGER.info("completed task=%s", task.task_id)


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
