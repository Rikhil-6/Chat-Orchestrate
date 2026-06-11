from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .artifacts import preview_command, scan_project_artifacts
from .models import ProjectSpace


FILE_MANIFEST_FENCE = "chat-orchestrate-files"
MAX_CONTEXT_CHARS = 50000
MAX_FILE_CHARS = 12000
READABLE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".ts",
    ".tsx",
    ".txt",
    ".yml",
    ".yaml",
}
BLOCKED_PARTS = {".git", ".chat-orchestrate", "node_modules", "dist", "build", "__pycache__"}
COMMON_FILE_NAMES = {"Dockerfile", "Makefile", "README", "README.md", "package.json", "pyproject.toml"}


@dataclass(frozen=True)
class AppliedFile:
    relative_path: str
    size: int


@dataclass(frozen=True)
class ApiHarnessResult:
    content: str
    applied_files: list[AppliedFile]


def build_api_harness_prompt(prompt: str, project: ProjectSpace | None) -> str:
    if project is None:
        return prompt
    return (
        f"{prompt}\n\n"
        "API fallback harness:\n"
        "- You are being called through a local harness that can write files for you.\n"
        "- If the user is asking for implementation, fixes, styling, backend routes, tests, or code changes, "
        "return a normal concise answer and exactly one fenced file manifest.\n"
        f"- The file manifest fence must be ```{FILE_MANIFEST_FENCE} containing JSON with this shape:\n"
        '{"summary":"short summary","files":[{"path":"frontend/app.js","content":"full file content"}],'
        '"commands":["optional preview or test commands"]}\n'
        "- Use relative paths only. Include full replacement file content, not patches.\n"
        "- If you naturally present files as markdown headings followed by fenced code blocks, put the relative "
        "file path in the heading immediately before each code block.\n"
        "- Do not write outside the active project workspace.\n"
        "- If no file change is needed, answer normally without a manifest.\n\n"
        f"Active project workspace: {project.path.resolve()}\n\n"
        f"{workspace_context(project)}"
    )


def workspace_context(project: ProjectSpace) -> str:
    project.path.mkdir(parents=True, exist_ok=True)
    artifacts = scan_project_artifacts(project, limit=10)
    if not artifacts:
        return "Workspace snapshot: no readable project artifacts yet."

    chunks = ["Workspace snapshot:"]
    used = 0
    for artifact in artifacts:
        path = Path(artifact.absolute_path)
        if path.suffix.lower() not in READABLE_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n..."
        entry = f"\n--- {artifact.relative_path} ---\n{content}"
        if used + len(entry) > MAX_CONTEXT_CHARS:
            chunks.append("\n--- snapshot truncated ---")
            break
        chunks.append(entry)
        used += len(entry)
    return "\n".join(chunks)


def apply_api_harness_response(project: ProjectSpace | None, response: str) -> ApiHarnessResult:
    if project is None:
        return ApiHarnessResult(response.strip(), [])
    payload, span = _extract_file_manifest(response)
    markdown_files = []
    markdown_spans: list[tuple[int, int]] = []
    if payload:
        file_items = payload.get("files", [])
        clean_response = _remove_manifest(response, span).strip()
        summary = str(payload.get("summary", "") or "").strip()
        commands = [str(command).strip() for command in payload.get("commands", []) if str(command).strip()]
    else:
        markdown_files = _extract_markdown_file_blocks(response)
        markdown_spans = [item["span"] for item in markdown_files]
        file_items = markdown_files
        clean_response = _remove_spans(response, markdown_spans).strip()
        summary = "Applied file blocks from the model response." if markdown_files else ""
        commands = []
    if not file_items:
        return ApiHarnessResult(response.strip(), [])

    applied = []
    for item in file_items:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path", "")).strip().replace("\\", "/")
        content = item.get("content")
        if not relative or not isinstance(content, str):
            continue
        target = _safe_workspace_path(project.path, relative)
        if target is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        applied.append(AppliedFile(relative, len(content.encode("utf-8"))))

    content = clean_response or summary or "Applied workspace changes."
    if applied:
        lines = [content, "", "### Applied Workspace Changes"]
        if summary and summary not in content:
            lines.append(summary)
        lines.extend(f"- `{item.relative_path}` ({item.size} bytes)" for item in applied)
        if commands:
            lines.extend(["", "Suggested commands:"])
            lines.extend(f"- `{command}`" for command in commands[:4])
        if any(item.relative_path == "frontend/index.html" for item in applied):
            lines.extend(["", f"Preview: `{preview_command(project)}`"])
        content = "\n".join(lines)
    return ApiHarnessResult(content, applied)


def _extract_file_manifest(response: str) -> tuple[dict | None, tuple[int, int] | None]:
    fence_pattern = re.compile(r"```(?:json|" + re.escape(FILE_MANIFEST_FENCE) + r")\s*(\{.*?\})\s*```", re.DOTALL)
    for match in fence_pattern.finditer(response):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("files"), list):
            return payload, match.span()
    stripped = response.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None, None
        if isinstance(payload, dict) and isinstance(payload.get("files"), list):
            return payload, (0, len(response))
    return None, None


def _remove_manifest(response: str, span: tuple[int, int] | None) -> str:
    if span is None:
        return response
    start, end = span
    return f"{response[:start]}{response[end:]}"


def _remove_spans(response: str, spans: list[tuple[int, int]]) -> str:
    result = response
    for start, end in sorted(spans, reverse=True):
        result = f"{result[:start]}{result[end:]}"
    return result


def _extract_markdown_file_blocks(response: str) -> list[dict]:
    blocks = []
    fence_pattern = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
    for match in fence_pattern.finditer(response):
        info = match.group(1).strip()
        content = match.group(2)
        if info == FILE_MANIFEST_FENCE:
            continue
        relative = _path_from_code_context(info, response[: match.start()])
        if not relative:
            continue
        blocks.append({"path": relative, "content": content, "span": match.span()})
    return blocks


def _path_from_code_context(info: str, prefix: str) -> str | None:
    for raw in _context_candidates(info):
        candidate = _clean_path_candidate(raw)
        if candidate:
            return candidate

    lines = [line.strip() for line in prefix.splitlines()[-8:] if line.strip()]
    for line in reversed(lines):
        for raw in _context_candidates(line):
            candidate = _clean_path_candidate(raw)
            if candidate:
                return candidate
    return None


def _context_candidates(text: str) -> list[str]:
    candidates = []
    text = text.strip()
    if not text:
        return candidates
    candidates.append(text)
    candidates.extend(re.findall(r"`([^`]+)`", text))
    candidates.extend(re.findall(r"(?:file|path)\s*[:=]\s*([A-Za-z0-9_./\\@+ -]+)", text, flags=re.IGNORECASE))
    candidates.extend(
        re.findall(
            r"([A-Za-z0-9_.@+-]+(?:[\\/][A-Za-z0-9_.@+-]+)+(?:\.[A-Za-z0-9_+-]+)?)",
            text,
        )
    )
    return candidates


def _clean_path_candidate(raw: str) -> str | None:
    candidate = raw.strip().strip("*_#:-) ")
    candidate = re.sub(r"^\d+\s*[\).:-]\s*", "", candidate)
    candidate = candidate.replace("\\", "/")
    if "://" in candidate or candidate.startswith(("/", "~")):
        return None
    name = Path(candidate).name
    suffix = Path(candidate).suffix.lower()
    if "/" not in candidate and name not in COMMON_FILE_NAMES:
        return None
    if suffix and suffix not in READABLE_SUFFIXES and name not in COMMON_FILE_NAMES:
        return None
    if any(part in {"", ".", ".."} for part in Path(candidate).parts):
        return None
    return candidate


def _safe_workspace_path(root: Path, relative: str) -> Path | None:
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    if any(part in BLOCKED_PARTS for part in candidate.parts):
        return None
    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return None
    return target
