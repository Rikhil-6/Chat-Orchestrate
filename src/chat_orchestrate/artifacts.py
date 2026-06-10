from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import ProjectSpace


SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".test-data", "data"}
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
        for path in root.rglob("*")
        if path.is_file() and _is_artifact_candidate(path, root) and path not in selected
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    result = []
    seen = set()
    for path in [*selected, *candidates]:
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        result.append(project_artifact(path, root))
        if len(result) >= limit:
            break
    return result


def project_artifact(path: Path, root: Path) -> ProjectArtifact:
    stat = path.stat()
    relative = path.relative_to(root).as_posix()
    return ProjectArtifact(
        label=_artifact_label(relative, path),
        kind=_artifact_kind(relative, path),
        relative_path=relative,
        absolute_path=str(path),
        size=stat.st_size,
        updated_at=datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        preview_url=_preview_url(relative),
    )


def preview_command(project: ProjectSpace | None) -> str:
    name = project.name if project else "default"
    return f"python scripts/preview_workspace.py --workspace {name}"


def artifact_chat_summary(project: ProjectSpace | None, limit: int = 5) -> str:
    artifacts = scan_project_artifacts(project, limit=limit)
    if project is None or not artifacts:
        return ""

    lines = [
        f"Code lives at `{project.path}`.",
        "Most useful artifacts:",
    ]
    for artifact in artifacts[:limit]:
        lines.append(f"- `{artifact.relative_path}` ({artifact.kind})")
    if any(artifact.relative_path == "frontend/index.html" for artifact in artifacts):
        lines.append(f"Interactive preview: `{preview_command(project)}` then open `http://localhost:5173`.")
    return "\n".join(lines)


def artifacts_markdown(project: ProjectSpace | None) -> str:
    if project is None:
        return "No active project space selected."
    artifacts = scan_project_artifacts(project, limit=16)
    if not artifacts:
        return f"No generated artifacts found yet under `{project.path}`."

    lines = [
        "## Project Artifacts",
        "",
        f"Workspace: `{project.path}`",
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
            "Then open `http://localhost:5173`.",
        ]
    )
    return "\n".join(lines)


def _is_artifact_candidate(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in SKIP_PARTS for part in relative.parts):
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    return path.suffix.lower() in CODE_SUFFIXES | DOC_SUFFIXES


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


def _preview_url(relative: str) -> str:
    if relative == "frontend/index.html":
        return "http://localhost:5173"
    if relative == "backend/app.py":
        return "http://localhost:8000/api/health"
    return ""
