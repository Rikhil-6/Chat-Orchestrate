from datetime import UTC, datetime
from pathlib import Path

from chat_orchestrate.backends import task_workspace_path
from chat_orchestrate.models import DelegatedTask
from chat_orchestrate.project_space import ProjectSpaceManager


def test_upsert_creates_relative_space(tmp_path: Path) -> None:
    manager = ProjectSpaceManager(tmp_path / "workspaces", tmp_path / "state.json")

    space = manager.upsert("My App", "apps/my-app")

    assert space.name == "my-app"
    assert space.mode == "local"
    assert space.path.exists()
    assert space.path == (tmp_path / "workspaces" / "apps" / "my-app").resolve()


def test_list_spaces_round_trips_state(tmp_path: Path) -> None:
    manager = ProjectSpaceManager(tmp_path / "workspaces", tmp_path / "state.json")
    manager.upsert("alpha", "alpha")

    reloaded = ProjectSpaceManager(tmp_path / "workspaces", tmp_path / "state.json")

    assert [space.name for space in reloaded.list_spaces()] == ["alpha"]


def test_upsert_preserves_workspace_mode(tmp_path: Path) -> None:
    manager = ProjectSpaceManager(tmp_path / "workspaces", tmp_path / "state.json")

    manager.upsert("clone-a", "clone-a", mode="clone", source="https://example.test/repo.git")

    space = manager.get("clone-a")
    assert space.mode == "clone"
    assert space.source == "https://example.test/repo.git"


def test_task_workspace_path_creates_missing_project_folder(tmp_path: Path) -> None:
    task = DelegatedTask(
        task_id="task-1",
        run_id="run-1",
        project="new-project",
        goal="build a website",
        role="frontend",
        title="Frontend pass",
        assigned_machine="local",
        preferred_backend="codex",
        status="delegated",
        created_at=datetime.now(UTC),
    )

    workspace = task_workspace_path(task, tmp_path / "workspaces")

    assert workspace == (tmp_path / "workspaces" / "new-project").resolve()
    assert workspace.exists()
