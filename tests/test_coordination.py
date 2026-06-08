from pathlib import Path

from chat_orchestrate.backends import CODEX_BACKEND, command_for_backend, extract_response_text, run_task
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


def test_delegation_honors_backend_frontend_machine_hints(tmp_path: Path) -> None:
    state = tmp_path / "coordination.json"
    host = CoordinationManager(
        state,
        "sg-akc-dt330",
        ["coordinator", "backend", "frontend"],
        ["codex"],
        cluster_id="test",
        coordination_token="secret",
    )
    remote = CoordinationManager(
        state,
        "desktop-p4k08ab",
        ["coordinator", "backend", "frontend"],
        ["claude-code"],
        cluster_id="test",
        coordination_token="secret",
    )
    host.claim_orchestrator()
    remote.heartbeat()

    tasks = host.plan_delegation(
        "run-1",
        ProjectSpace(name="demo", path=tmp_path / "demo"),
        "have this machine work on a backend and delegate the frontend building to desktop-p4k08ab",
    )
    by_role = {task.role: task for task in tasks}

    assert by_role["backend"].assigned_machine == "sg-akc-dt330"
    assert by_role["backend"].preferred_backend == "codex"
    assert by_role["frontend"].assigned_machine == "desktop-p4k08ab"
    assert by_role["frontend"].preferred_backend == "claude-code"


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


def test_command_override_accepts_quoted_paths(tmp_path: Path) -> None:
    executable = tmp_path / "codex.cmd"
    executable.write_text("@echo off\r\necho codex help\r\nexit /B 0\r\n", encoding="utf-8")

    assert command_for_backend(CODEX_BACKEND, {CODEX_BACKEND: f'"{executable}"'}) == str(executable)


def test_command_override_rejects_config_directories(tmp_path: Path) -> None:
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()

    assert command_for_backend(CODEX_BACKEND, {CODEX_BACKEND: str(config_dir)}) is None


def test_none_command_override_falls_back_to_auto_lookup(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "codex.cmd"
    executable.write_text("@echo off\r\necho codex help\r\nexit /B 0\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert command_for_backend(CODEX_BACKEND, {CODEX_BACKEND: "None"}).lower() == str(executable).lower()


def test_windows_store_codex_resource_is_not_treated_as_cli(tmp_path: Path, monkeypatch) -> None:
    resource_dir = tmp_path / "WindowsApps" / "OpenAI.Codex_1.0.0_x64__abc" / "app" / "resources"
    resource_dir.mkdir(parents=True)
    (resource_dir / "codex.cmd").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(resource_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf86"))

    assert command_for_backend(CODEX_BACKEND, {CODEX_BACKEND: "None"}) is None


def test_extract_response_text_from_output_text() -> None:
    assert extract_response_text({"output_text": "hello"}) == "hello"


def test_extract_response_text_from_output_content() -> None:
    payload = {"output": [{"content": [{"type": "output_text", "text": "hello from codex"}]}]}

    assert extract_response_text(payload) == "hello from codex"


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


def test_corrupt_state_with_extra_json_is_salvaged(tmp_path: Path) -> None:
    state = tmp_path / "coordination.json"
    state.write_text(
        '{"cluster_id":"local","token_hash":"","machines":{},"tasks":[]}'
        '  }\n],"cluster_id":"local"}',
        encoding="utf-8",
    )
    manager = CoordinationManager(state, "machine-a", ["coordinator"], ["simulated"])

    node = manager.heartbeat()

    assert node.machine_id == "machine-a"
    assert manager.list_machines()[0].machine_id == "machine-a"
    assert not state.read_text(encoding="utf-8").strip().endswith('],"cluster_id":"local"}')
    assert list(tmp_path.glob("coordination.corrupt-*.json"))
