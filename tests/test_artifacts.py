from pathlib import Path

from datetime import UTC, datetime

from chat_orchestrate.artifacts import (
    artifact_chat_summary,
    build_evaluation,
    build_evaluation_summary,
    preview_command,
    preview_frontend_url,
    save_project_preview_ports,
    scan_project_artifacts,
    task_completion_stats,
    work_proof_summary,
    workspace_layout_markdown,
)
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
    (workspace / ".chat-orchestrate").mkdir()
    (workspace / ".chat-orchestrate" / "codex-final.md").write_text("ignore", encoding="utf-8")

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
    assert ".chat-orchestrate/codex-final.md" not in paths
    assert artifacts[0].preview_url == "http://localhost:5173"


def test_artifact_chat_summary_points_to_workspace_and_preview(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "frontend" / "index.html").write_text("<html></html>", encoding="utf-8")
    project = ProjectSpace("demo", workspace)

    summary = artifact_chat_summary(project)

    assert str(workspace) in summary
    assert "App checkout" in summary
    assert "frontend/index.html" in summary
    assert preview_command(project) in summary


def test_preview_command_uses_absolute_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    project = ProjectSpace("demo", workspace)

    command = preview_command(project)

    assert "scripts" in command
    assert "preview_workspace.py" in command
    assert str(workspace.resolve()) in command
    assert "--workspace" in command
    assert "--frontend-port 5173" in command
    assert "--backend-port 8000" in command


def test_preview_command_uses_saved_project_ports(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    project = ProjectSpace("demo", workspace)

    save_project_preview_ports(project, frontend_port=5200, backend_port=8010)
    command = preview_command(project)

    assert "--frontend-port 5200" in command
    assert "--backend-port 8010" in command
    assert preview_frontend_url(project) == "http://localhost:5200"


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


def test_build_evaluation_summary_reports_frontend_backend_and_task_counts(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / "backend").mkdir()
    (workspace / "frontend" / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / "frontend" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (workspace / "frontend" / "styles.css").write_text("body {}", encoding="utf-8")
    (workspace / "backend" / "app.py").write_text("app = object()", encoding="utf-8")
    project = ProjectSpace("demo", workspace)
    tasks = [
        DelegatedTask(
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
        ),
        DelegatedTask(
            "task-2",
            "run-1",
            "demo",
            "build site",
            "backend",
            "Backend pass",
            "machine-b",
            "codex",
            "running",
            datetime.now(UTC),
        ),
    ]

    summary = build_evaluation_summary(project, tasks)
    evaluation = build_evaluation(project, tasks)
    stats = task_completion_stats(tasks)

    assert "Build Evaluation" in summary
    assert "Frontend: `ready`" in summary
    assert "Backend: `ready`" in summary
    assert "Agent tasks: `1/2` completed" in summary
    assert evaluation["frontend_status"] == "ready"
    assert evaluation["backend_status"] == "ready"
    assert evaluation["task_stats"]["total"] == 2
    assert stats["completed"] == 1
    assert stats["running"] == 1


def test_workspace_layout_markdown_separates_source_runtime_and_project(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    (workspace / "frontend").mkdir(parents=True)
    (workspace / ".chat-orchestrate").mkdir()
    (workspace / "frontend" / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / ".chat-orchestrate" / "codex-final.md").write_text("internal", encoding="utf-8")
    project = ProjectSpace("demo", workspace)

    layout = workspace_layout_markdown(project)

    assert "App checkout" in layout
    assert "Active workspace" in layout
    assert "Harness Source" in layout
    assert "Generated Project" in layout
    assert "Harness Runtime" in layout
    assert "frontend/index.html" in layout
    assert ".chat-orchestrate/codex-final.md" not in layout
