from pathlib import Path

from chat_orchestrate.backends import run_task
from chat_orchestrate.coordination import CoordinationManager
from chat_orchestrate.models import ProjectSpace


def test_claim_orchestrator_marks_local_machine(tmp_path: Path) -> None:
    manager = CoordinationManager(
        tmp_path / "coordination.json",
        "machine-a",
        ["coordinator"],
        ["claude-code"],
        cluster_id="test",
        coordination_token="secret",
    )

    node = manager.claim_orchestrator()

    assert node.machine_id == "machine-a"
    assert node.role == "orchestrator"


def test_delegation_assigns_tasks_to_registered_machines(tmp_path: Path) -> None:
    state = tmp_path / "coordination.json"
    manager_a = CoordinationManager(
        state,
        "machine-a",
        ["coordinator", "reviewer"],
        ["claude-code"],
        cluster_id="test",
        coordination_token="secret",
    )
    manager_b = CoordinationManager(
        state,
        "machine-b",
        ["engineer", "documenter"],
        ["codex"],
        cluster_id="test",
        coordination_token="secret",
    )
    manager_a.claim_orchestrator()
    manager_b.heartbeat()

    tasks = manager_a.plan_delegation(
        "run-1",
        ProjectSpace(name="demo", path=tmp_path / "demo"),
        "build and document the distributed runner",
    )

    assert tasks
    assert {task.assigned_machine for task in tasks}.issubset({"machine-a", "machine-b"})
    assert any(task.preferred_backend == "codex" for task in tasks)


def test_worker_claims_and_completes_matching_backend_task(tmp_path: Path) -> None:
    state = tmp_path / "coordination.json"
    manager = CoordinationManager(state, "machine-a", ["engineer"], ["codex"])
    manager.heartbeat()
    manager.plan_delegation(
        "run-1",
        ProjectSpace(name="demo", path=tmp_path / "demo"),
        "build the feature with codex",
    )

    task = manager.claim_next_task()

    assert task is not None
    assert task.preferred_backend == "codex"
    manager.complete_task(task.task_id, run_task(task, dry_run=True))
    assert manager.list_tasks()[0].status == "completed"


def test_coordination_token_mismatch_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "coordination.json"
    manager_a = CoordinationManager(
        state,
        "machine-a",
        ["coordinator"],
        ["simulated"],
        cluster_id="friends",
        coordination_token="correct",
    )
    manager_a.heartbeat()
    manager_b = CoordinationManager(
        state,
        "machine-b",
        ["coordinator"],
        ["simulated"],
        cluster_id="friends",
        coordination_token="wrong",
    )

    try:
        manager_b.heartbeat()
    except Exception as exc:
        assert "token" in str(exc).lower()
    else:
        raise AssertionError("Expected token mismatch to be rejected.")
