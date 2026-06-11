from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .models import ProjectSpace


SKIP_PARTS = {
    "__pycache__",
    ".git",
    ".cache",
    ".mypy_cache",
    ".pytest-tmp",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".test-data",
    ".chat-orchestrate",
    "dist",
    "build",
    "data",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}
SKIP_SUFFIXES = {".pyc", ".sqlite3", ".db", ".log"}
CODE_SUFFIXES = {".py", ".js", ".css", ".html", ".ts", ".tsx", ".jsx", ".json"}
DOC_SUFFIXES = {".md", ".txt"}
PRIORITY_PATHS = [
    "frontend/index.html",
    "frontend/app.js",
    "frontend/styles.css",
    "backend/app.py",
    "backend/db.py",
    "backend/tests/test_api.py",
]
PROJECT_RUNTIME_FILE = ".chat-orchestrate.json"
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_BACKEND_PORT = 8000


def app_root() -> Path:
    return Path(__file__).resolve().parents[2]


def shell_quote(value: str | Path) -> str:
    text = str(value)
    escaped = text.replace('"', '\\"')
    return f'"{escaped}"'


def project_runtime_config_path(project: ProjectSpace | None) -> Path | None:
    if project is None:
        return None
    return project.path / PROJECT_RUNTIME_FILE


def load_project_runtime_config(project: ProjectSpace | None) -> dict:
    config_path = project_runtime_config_path(project)
    if config_path is None or not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_project_preview_ports(
    project: ProjectSpace,
    frontend_port: int | None = None,
    backend_port: int | None = None,
) -> None:
    project.path.mkdir(parents=True, exist_ok=True)
    config_path = project_runtime_config_path(project)
    if config_path is None:
        return
    payload = load_project_runtime_config(project)
    if frontend_port is not None:
        payload["frontend_port"] = int(frontend_port)
    if backend_port is not None:
        payload["backend_port"] = int(backend_port)
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def preview_ports(project: ProjectSpace | None) -> tuple[int, int]:
    payload = load_project_runtime_config(project)
    return (
        _clean_port(payload.get("frontend_port"), DEFAULT_FRONTEND_PORT),
        _clean_port(payload.get("backend_port"), DEFAULT_BACKEND_PORT),
    )


def preview_frontend_url(project: ProjectSpace | None) -> str:
    frontend_port, _ = preview_ports(project)
    return f"http://localhost:{frontend_port}"


def preview_backend_url(project: ProjectSpace | None) -> str:
    _, backend_port = preview_ports(project)
    return f"http://localhost:{backend_port}/api/health"


@dataclass(frozen=True)
class ProjectArtifact:
    label: str
    kind: str
    relative_path: str
    absolute_path: str
    size: int
    updated_at: str
    preview_url: str = ""


def scan_project_artifacts(project: ProjectSpace | None, limit: int = 12) -> list[ProjectArtifact]:
    if project is None or not project.path.exists():
        return []

    root = project.path.resolve()
    selected: list[Path] = []
    for relative in PRIORITY_PATHS:
        candidate = root / relative
        if candidate.exists() and candidate.is_file():
            selected.append(candidate)

    candidates = [
        path
        for path in _iter_candidate_files(root)
        if _is_artifact_candidate(path, root) and path not in selected
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    result = []
    seen = set()
    for path in [*selected, *candidates]:
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        result.append(project_artifact(path, root, project))
        if len(result) >= limit:
            break
    return result


def project_artifact(path: Path, root: Path, project: ProjectSpace | None = None) -> ProjectArtifact:
    stat = path.stat()
    relative = path.relative_to(root).as_posix()
    return ProjectArtifact(
        label=_artifact_label(relative, path),
        kind=_artifact_kind(relative, path),
        relative_path=relative,
        absolute_path=str(path),
        size=stat.st_size,
        updated_at=datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        preview_url=_preview_url(relative, project),
    )


def preview_command(project: ProjectSpace | None) -> str:
    workspace = project.path.resolve() if project else (app_root() / "workspaces" / "default").resolve()
    script = app_root() / "scripts" / "preview_workspace.py"
    frontend_port, backend_port = preview_ports(project)
    return (
        f"python {shell_quote(script)} --workspace {shell_quote(workspace)} "
        f"--frontend-port {frontend_port} --backend-port {backend_port}"
    )


def artifact_chat_summary(project: ProjectSpace | None, limit: int = 5) -> str:
    artifacts = scan_project_artifacts(project, limit=limit)
    if project is None or not artifacts:
        return ""
    workspace = project.path.resolve()

    lines = [
        "### Work Proof",
        f"App checkout: `{app_root()}`",
        f"Workspace: `{workspace}`",
        "",
        "Code artifacts:",
    ]
    for artifact in artifacts[:limit]:
        lines.append(f"- `{artifact.relative_path}` ({artifact.kind}, updated {artifact.updated_at})")
    if any(artifact.relative_path == "frontend/index.html" for artifact in artifacts):
        lines.extend(
            [
                "",
                f"Preview from any terminal: `{preview_command(project)}` then open `{preview_frontend_url(project)}`.",
            ]
        )
    return "\n".join(lines)


def agent_work_summary(tasks: Iterable[object] | None = None, limit: int = 5) -> str:
    if not tasks:
        return ""
    lines = []
    for task in list(tasks)[:limit]:
        if isinstance(task, dict):
            role = task.get("role", "")
            machine = task.get("machine", "") or task.get("assigned_machine", "")
            backend = task.get("backend", "") or task.get("preferred_backend", "")
            status = task.get("status", "")
        else:
            role = getattr(task, "role", "")
            machine = getattr(task, "assigned_machine", "") or getattr(task, "machine", "")
            backend = getattr(task, "preferred_backend", "") or getattr(task, "backend", "")
            status = getattr(task, "status", "")
        clean = " ".join(part for part in [str(role), "on", str(machine), f"via {backend}" if backend else "", f"({status})" if status else ""] if part)
        if clean:
            lines.append(f"- {clean}")
    if not lines:
        return ""
    return "Agent evidence:\n" + "\n".join(lines)


def work_proof_summary(project: ProjectSpace | None, tasks: Iterable[object] | None = None) -> str:
    artifacts = artifact_chat_summary(project)
    evidence = agent_work_summary(tasks)
    return "\n\n".join(part for part in [artifacts, evidence] if part)


def task_completion_stats(tasks: Iterable[object] | None = None) -> dict[str, int]:
    task_list = list(tasks or [])
    return {
        "total": len(task_list),
        "completed": sum(1 for task in task_list if _task_field(task, "status") == "completed"),
        "failed": sum(1 for task in task_list if _task_field(task, "status") == "failed"),
        "running": sum(1 for task in task_list if _task_field(task, "status") == "running"),
        "delegated": sum(1 for task in task_list if _task_field(task, "status") == "delegated"),
    }


def build_evaluation_summary(project: ProjectSpace | None, tasks: Iterable[object] | None = None) -> str:
    if project is None:
        return ""
    evaluation = build_evaluation(project, tasks)
    frontend_files = evaluation["frontend_files"]
    backend_files = evaluation["backend_files"]
    stats = evaluation["task_stats"]

    lines = ["### Build Evaluation"]
    lines.append(f"- Frontend: `{evaluation['frontend_status']}` - {evaluation['frontend_detail']}.")
    lines.append(f"- Backend: `{evaluation['backend_status']}` - {evaluation['backend_detail']}.")
    if stats["total"]:
        extras = []
        if stats["running"]:
            extras.append(f"{stats['running']} running")
        if stats["delegated"]:
            extras.append(f"{stats['delegated']} queued")
        if stats["failed"]:
            extras.append(f"{stats['failed']} failed")
        suffix = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"- Agent tasks: `{stats['completed']}/{stats['total']}` completed{suffix}.")
    if frontend_files:
        lines.append(f"- Preview: `{preview_frontend_url(project)}` via `{preview_command(project)}`.")
    return "\n".join(lines)


def build_evaluation(project: ProjectSpace | None, tasks: Iterable[object] | None = None) -> dict:
    artifacts = scan_project_artifacts(project, limit=16)
    paths = {artifact.relative_path for artifact in artifacts}
    frontend_files = [path for path in ["frontend/index.html", "frontend/app.js", "frontend/styles.css"] if path in paths]
    backend_files = [path for path in ["backend/app.py", "backend/tests/test_api.py"] if path in paths]
    return {
        "frontend_status": _surface_status(frontend_files, "frontend/index.html", expected_count=3),
        "frontend_detail": _surface_detail(frontend_files, "frontend"),
        "frontend_files": frontend_files,
        "backend_status": _surface_status(backend_files, "backend/app.py", expected_count=1),
        "backend_detail": _surface_detail(backend_files, "backend"),
        "backend_files": backend_files,
        "task_stats": task_completion_stats(tasks),
    }


def artifacts_markdown(project: ProjectSpace | None) -> str:
    if project is None:
        return "No active project space selected."
    artifacts = scan_project_artifacts(project, limit=16)
    if not artifacts:
        return f"No generated artifacts found yet under `{project.path.resolve()}`."

    lines = [
        "## Project Artifacts",
        "",
        f"Workspace: `{project.path.resolve()}`",
        f"App checkout: `{app_root()}`",
        "",
        "| Kind | File | Updated |",
        "| --- | --- | --- |",
    ]
    for artifact in artifacts:
        lines.append(f"| {artifact.kind} | `{artifact.relative_path}` | {artifact.updated_at} |")
    lines.extend(
        [
            "",
            "Run an interactive local preview with:",
            "",
            "```powershell",
            preview_command(project),
            "```",
            "",
            f"Then open `{preview_frontend_url(project)}`.",
        ]
    )
    return "\n".join(lines)


def _task_field(task: object, field: str) -> str:
    if isinstance(task, dict):
        return str(task.get(field, "") or "")
    return str(getattr(task, field, "") or "")


def _surface_status(files: list[str], required: str, expected_count: int) -> str:
    if required in files and len(files) >= expected_count:
        return "ready"
    if required in files:
        return "partial"
    return "missing"


def _surface_detail(files: list[str], label: str) -> str:
    if not files:
        return f"no {label} artifacts found yet"
    return "found " + ", ".join(f"`{path}`" for path in files)


def workspace_layout_markdown(project: ProjectSpace | None) -> str:
    workspace = project.path.resolve() if project else (app_root() / "workspaces" / "default").resolve()
    artifacts = scan_project_artifacts(project, limit=12)
    lines = [
        "## File Layout",
        "",
        f"App checkout: `{app_root()}`",
        f"Active workspace: `{workspace}`",
        "",
        "### Harness Source",
        "",
        "- `src/chat_orchestrate/` - Chainlit app, coordination, local-agent bridge, workspace logic",
        "- `public/` - dashboard/sidebar UI assets loaded by Chainlit",
        "- `scripts/` - launch, coordinator, worker, and preview scripts",
        "- `docs/` and `tests/` - operator notes and verification",
        "",
        "### Generated Project",
        "",
        f"Project code should be under `{workspace}`:",
        "",
        "- `frontend/index.html`, `frontend/app.js`, `frontend/styles.css`",
        "- `backend/app.py`",
        "- `README.generated.md`",
        "",
        "### Harness Runtime",
        "",
        f"- `{workspace / '.chat-orchestrate'}` stores internal local-agent final-message files.",
        "- `coordination_state.json`, `runtime_config.json`, `ui_state.json`, and `workspace_state.json` are local runtime state.",
    ]
    root_frontend = app_root() / "frontend"
    if root_frontend.exists():
        lines.extend(
            [
                "",
                "### Layout Note",
                "",
                f"`{root_frontend}` exists, but root-level generated app folders are ignored. "
                "Use the active workspace path above for current project code.",
            ]
        )
    if artifacts:
        lines.extend(["", "### Current Artifacts", ""])
        for artifact in artifacts:
            lines.append(f"- `{artifact.relative_path}` ({artifact.kind}, updated {artifact.updated_at})")
        if any(artifact.relative_path == "frontend/index.html" for artifact in artifacts):
            lines.extend(["", f"Preview: `{preview_command(project)}`"])
    else:
        lines.extend(["", "### Current Artifacts", "", "No visible project artifacts found in the active workspace yet."])
    return "\n".join(lines)


def _is_artifact_candidate(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if path.name == PROJECT_RUNTIME_FILE:
        return False
    if any(part in SKIP_PARTS for part in relative.parts):
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    return path.suffix.lower() in CODE_SUFFIXES | DOC_SUFFIXES


def _iter_candidate_files(root: Path) -> Iterable[Path]:
    """Walk project files without letting stale runtime dirs break artifact scans."""
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name in SKIP_PARTS:
                continue
            try:
                if child.is_dir():
                    pending.append(child)
                elif child.is_file():
                    yield child
            except OSError:
                continue


def _artifact_kind(relative: str, path: Path) -> str:
    suffix = path.suffix.lower()
    if relative == "frontend/index.html":
        return "frontend entry"
    if relative.startswith("frontend/"):
        return "frontend code"
    if relative == "backend/app.py":
        return "backend api"
    if relative.startswith("backend/tests/"):
        return "backend tests"
    if relative.startswith("backend/"):
        return "backend code"
    if suffix in DOC_SUFFIXES:
        return "handoff doc"
    return "code"


def _artifact_label(relative: str, path: Path) -> str:
    if relative == "frontend/index.html":
        return "Frontend app"
    if relative == "backend/app.py":
        return "Backend API"
    if path.suffix.lower() == ".md":
        return path.stem.replace("_", " ").replace("-", " ").title()
    return path.name


def _preview_url(relative: str, project: ProjectSpace | None = None) -> str:
    if relative == "frontend/index.html":
        return preview_frontend_url(project)
    if relative == "backend/app.py":
        return preview_backend_url(project)
    return ""


def _clean_port(value: object, fallback: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return fallback
    if 1 <= port <= 65535:
        return port
    return fallback
