from pathlib import Path

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
