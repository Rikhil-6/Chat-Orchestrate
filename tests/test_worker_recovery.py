import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from chat_orchestrate.coordination import CoordinationError
from chat_orchestrate.models import DelegatedTask
from chat_orchestrate.worker import (
    _append_pending_result,
    _flush_pending_results,
    _read_pending_results,
    _run_task_with_lease,
)


class _RecorderCoordination:
    def __init__(self, fail_complete: bool = False) -> None:
        self.renew_count = 0
        self.completed: list[tuple[str, str, str]] = []
        self.completion_meta: list[tuple[str, str]] = []
        self.fail_complete = fail_complete

    def renew_task_lease(self, task_id: str) -> bool:  # noqa: ARG002
        self.renew_count += 1
        return True

    def note_task_progress(self, task_id: str, note: str, status: str | None = None) -> bool:  # noqa: ARG002
        return True

    def complete_task(
        self,
        task_id: str,
        result: str,
        status: str = "completed",
        *,
        completed_by: str = "",
        completion_source: str = "direct",
    ) -> None:
        if self.fail_complete:
            raise CoordinationError("down")
        self.completed.append((task_id, status, result))
        self.completion_meta.append((completed_by, completion_source))


def _sample_task() -> DelegatedTask:
    now = datetime.now(UTC)
    return DelegatedTask(
        task_id="task-1",
        run_id="run-1",
        project="demo",
        goal="demo goal",
        role="engineer",
        title="Engineer pass",
        assigned_machine="machine-a",
        preferred_backend="simulated",
        status="running",
        created_at=now,
        updated_at=now,
    )


def test_pending_results_replay_clears_queue(tmp_path: Path) -> None:
    path = tmp_path / ".pending-results.jsonl"
    _append_pending_result(path, "task-1", "done", "completed", machine_id="machine-a")
    _append_pending_result(path, "task-2", "boom", "failed", machine_id="machine-a")
    assert len(_read_pending_results(path)) == 2

    recorder = _RecorderCoordination()
    _flush_pending_results(recorder, path)

    assert recorder.completed == [("task-1", "completed", "done"), ("task-2", "failed", "boom")]
    assert recorder.completion_meta == [("machine-a", "replayed"), ("machine-a", "replayed")]
    assert not path.exists()


def test_pending_results_replay_keeps_queue_when_unreachable(tmp_path: Path) -> None:
    path = tmp_path / ".pending-results.jsonl"
    _append_pending_result(path, "task-1", "done", "completed", machine_id="machine-a")
    recorder = _RecorderCoordination(fail_complete=True)

    _flush_pending_results(recorder, path)

    assert path.exists()
    assert len(_read_pending_results(path)) == 1


def test_run_task_with_lease_renews_until_finished(monkeypatch) -> None:
    async def run() -> tuple[str, int]:
        task = _sample_task()
        recorder = _RecorderCoordination()

        def fake_run_task(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            import time

            time.sleep(0.15)
            return "ok"

        monkeypatch.setattr("chat_orchestrate.worker.run_task", fake_run_task)

        settings = SimpleNamespace(
            worker_dry_run=False,
            command_overrides={},
            openai_api_key="",
            codex_api_model="gpt-5.3-codex",
            claude_api_key="",
            gemini_api_key="",
            claude_api_model="claude-sonnet-4-5",
            gemini_api_model="gemini-2.0-flash",
            workspaces_root=Path("."),
            worker_poll_seconds=0.05,
        )
        result = await _run_task_with_lease(task, recorder, settings)
        return result, recorder.renew_count

    result, renew_count = asyncio.run(run())
    assert result == "ok"
    assert renew_count >= 1
