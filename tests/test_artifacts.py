from pathlib import Path

from datetime import UTC, datetime

from chat_orchestrate.artifacts import artifact_chat_summary, preview_command, scan_project_artifacts, work_proof_summary
from chat_orchestrate.models import DelegatedTask, ProjectSpace


def test_scan_project_artifacts_prioritizes_generated_app_files(tmp_path: Path) -> None:
    workspace = tmp_path / "default"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "backend" / "tests").mkdir(parents=True)
    (workspace / "frontend" / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / "frontend" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (workspace / "backend" / "app.py").write_text("app = object()", encoding="utf-8")
    (workspace / "backend" / "tests" / "test_api.py").write_text("def test_ok(): pass", encoding="utf-8")
    (workspace / "backend" / "data").mkdir()
    (workspace / "backend" / "data" / "search.sqlite3").write_text("ignore", encoding="utf-8")

    project = ProjectSpace("default", workspace)
    artifacts = scan_project_artifacts(project)
    paths = [artifact.relative_path for artifact in artifacts]

    assert paths[:4] == [
        "frontend/index.html",
        "frontend/app.js",
        "backend/app.py",
        "backend/tests/test_api.py",
    ]
    assert "backend/data/search.sqlite3" not in paths
    assert artifacts[0].preview_url == "http://localhost:5173"


def test_artifact_chat_summary_points_to_workspace_and_preview(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "frontend" / "index.html").write_text("<html></html>", encoding="utf-8")
    project = ProjectSpace("demo", workspace)

    summary = artifact_chat_summary(project)

    assert str(workspace) in summary
    assert "frontend/index.html" in summary
    assert preview_command(project) in summary


def test_work_proof_summary_includes_files_and_agent_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "frontend" / "index.html").write_text("<html></html>", encoding="utf-8")
    project = ProjectSpace("demo", workspace)
    task = DelegatedTask(
        "task-1",
        "run-1",
        "demo",
        "build site",
        "frontend",
        "Frontend pass",
        "machine-a",
        "codex",
        "completed",
        datetime.now(UTC),
    )

    proof = work_proof_summary(project, [task])

    assert "Work Proof" in proof
    assert "frontend/index.html" in proof
    assert "frontend on machine-a via codex (completed)" in proof
