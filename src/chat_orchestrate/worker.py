from __future__ import annotations

import asyncio
import logging

from .backends import detect_agent_backends, run_task
from .config import Settings, get_settings
from .coordination import CoordinationManager


LOGGER = logging.getLogger("chat_orchestrate.worker")


def build_coordination(settings: Settings | None = None) -> CoordinationManager:
    settings = settings or get_settings()
    return CoordinationManager(
        settings.coordination_state_path,
        settings.machine_id,
        settings.default_agents,
        detect_agent_backends(settings.configured_backends),
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
    node = coordination.heartbeat()
    LOGGER.info(
        "worker started machine=%s backends=%s dry_run=%s",
        node.machine_id,
        ",".join(node.agent_backends),
        settings.worker_dry_run,
    )

    while True:
        coordination.heartbeat()
        task = coordination.claim_next_task()
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
            result = run_task(task, dry_run=settings.worker_dry_run)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            coordination.complete_task(task.task_id, str(exc), status="failed")
            LOGGER.exception("task failed task=%s", task.task_id)
        else:
            coordination.complete_task(task.task_id, result)
            LOGGER.info("completed task=%s", task.task_id)
