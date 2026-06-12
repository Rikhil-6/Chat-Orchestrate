import subprocess
from datetime import UTC, datetime
from pathlib import Path

from chat_orchestrate.backends import task_workspace_path
from chat_orchestrate.models import DelegatedTask
from chat_orchestrate.project_space import ProjectSpaceManager, parse_project_share_pack, project_share_pack


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


def test_bind_repository_reads_existing_git_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.test/demo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    manager = ProjectSpaceManager(tmp_path / "workspaces", tmp_path / "state.json")

    space = manager.bind_repository("Demo Repo", repo)

    assert space.name == "demo-repo"
    assert space.mode == "repo"
    assert space.path == repo.resolve()
    assert space.git_remote == "https://example.test/demo.git"


def test_share_pack_round_trip_preserves_git_details(tmp_path: Path) -> None:
    manager = ProjectSpaceManager(tmp_path / "workspaces", tmp_path / "state.json")
    space = manager.upsert(
        "alpha",
        "alpha",
        git_remote="https://github.com/example/alpha.git",
        mode="clone",
        source="https://github.com/example/alpha.git",
    )

    parsed = parse_project_share_pack(project_share_pack(space))

    assert parsed["project_name"] == "alpha"
    assert parsed["git_remote"] == "https://github.com/example/alpha.git"
    assert parsed["mode"] == "clone"


def test_upsert_writes_project_profile_manifest(tmp_path: Path) -> None:
    manager = ProjectSpaceManager(tmp_path / "workspaces", tmp_path / "state.json")

    space = manager.upsert(
        "alpha",
        "alpha",
        git_remote="https://github.com/example/alpha.git",
        mode="clone",
        visibility="private",
        source_kind="github",
    )

    manifest_path = space.path / "project_profile.json"

    assert manifest_path.exists()
    manifest = manifest_path.read_text(encoding="utf-8")
    assert '"project_name": "alpha"' in manifest
    assert '"source_kind": "github"' in manifest
    assert '"visibility": "private"' in manifest


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
