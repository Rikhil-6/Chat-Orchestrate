from __future__ import annotations

import json
import re
import secrets
import subprocess
from pathlib import Path

from .models import ProjectSpace


class ProjectSpaceError(RuntimeError):
    pass


class ProjectSpaceManager:
    """Stores and resolves named project spaces for chat sessions."""

    def __init__(self, root: Path, state_path: Path) -> None:
        self.root = root.resolve()
        self.state_path = state_path.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list_spaces(self) -> list[ProjectSpace]:
        return sorted(self._load().values(), key=lambda space: space.name)

    def get(self, name: str) -> ProjectSpace:
        spaces = self._load()
        try:
            return spaces[name]
        except KeyError as exc:
            raise ProjectSpaceError(f"Unknown project space: {name}") from exc

    def upsert(
        self,
        name: str,
        path: str | Path,
        git_remote: str | None = None,
        mode: str = "local",
        source: str | None = None,
        project_id: str | None = None,
        source_kind: str | None = None,
        visibility: str | None = None,
    ) -> ProjectSpace:
        clean_name = self._clean_name(name)
        resolved_path = self._resolve_project_path(path)
        resolved_path.mkdir(parents=True, exist_ok=True)

        branch = self._git_branch(resolved_path)
        existing = self._load().get(clean_name)
        manifest = self._load_manifest(resolved_path)
        space = ProjectSpace(
            name=clean_name,
            path=resolved_path,
            mode=mode,
            project_id=project_id or self._stable_project_id(clean_name, existing, manifest),
            source_kind=source_kind or self._infer_source_kind(git_remote or self._git_remote(resolved_path), mode, manifest, existing),
            visibility=visibility or self._infer_visibility(git_remote or self._git_remote(resolved_path), manifest, existing),
            git_remote=git_remote or self._git_remote(resolved_path),
            branch=branch,
            source=source,
        )

        spaces = self._load()
        spaces[clean_name] = space
        self._save(spaces)
        self._save_manifest(space)
        return space

    def create_worktree(
        self,
        name: str,
        repository_path: str | Path,
        branch: str,
        base_ref: str = "HEAD",
    ) -> ProjectSpace:
        clean_name = self._clean_name(name)
        repo = self._git_repo_root(Path(repository_path).expanduser().resolve())

        target = (self.root / clean_name).resolve()
        if target.exists() and any(target.iterdir()):
            raise ProjectSpaceError(f"Worktree target is not empty: {target}")

        self._run_git(["worktree", "add", "-b", branch, str(target), base_ref], cwd=repo)
        return self.upsert(clean_name, target, mode="worktree", source=str(repo))

    def bind_repository(
        self,
        name: str,
        repository_path: str | Path,
    ) -> ProjectSpace:
        clean_name = self._clean_name(name)
        repo = self._git_repo_root(Path(repository_path).expanduser().resolve())
        return self.upsert(clean_name, repo, mode="repo", source=str(repo))

    def update_profile(
        self,
        name: str,
        *,
        project_name: str | None = None,
        source_kind: str | None = None,
        visibility: str | None = None,
        git_remote: str | None = None,
    ) -> ProjectSpace:
        current = self.get(name)
        next_name = self._clean_name(project_name or current.name)
        space = self.upsert(
            next_name,
            current.path,
            git_remote=git_remote if git_remote is not None else current.git_remote,
            mode=current.mode,
            source=current.source,
            project_id=current.project_id,
            source_kind=source_kind or current.source_kind,
            visibility=visibility or current.visibility,
        )
        if next_name != current.name:
            spaces = self._load()
            spaces.pop(current.name, None)
            spaces[next_name] = space
            self._save(spaces)
        return space

    def ensure_git_repository(
        self,
        path: str | Path,
        default_branch: str = "main",
    ) -> Path:
        repo_path = self._resolve_project_path(path)
        repo_path.mkdir(parents=True, exist_ok=True)
        if (repo_path / ".git").exists():
            return repo_path
        self._run_git(["init", "-b", default_branch], cwd=repo_path)
        return repo_path

    def configure_remote(
        self,
        path: str | Path,
        git_remote: str,
        remote_name: str = "origin",
        default_branch: str = "main",
    ) -> Path:
        repo_path = self.ensure_git_repository(path, default_branch=default_branch)
        current = self._maybe_git(["remote", "get-url", remote_name], cwd=repo_path)
        if current:
            if current != git_remote:
                self._run_git(["remote", "set-url", remote_name, git_remote], cwd=repo_path)
        else:
            self._run_git(["remote", "add", remote_name, git_remote], cwd=repo_path)
        return repo_path

    def clone_repository(
        self,
        name: str,
        git_url: str,
        branch: str | None = None,
    ) -> ProjectSpace:
        clean_name = self._clean_name(name)
        target = (self.root / clean_name).resolve()
        if target.exists() and any(target.iterdir()):
            raise ProjectSpaceError(f"Clone target is not empty: {target}")

        args = ["clone"]
        if branch:
            args.extend(["--branch", branch])
        args.extend([git_url, str(target)])
        self._run_git(args, cwd=self.root)
        return self.upsert(clean_name, target, git_remote=git_url, mode="clone", source=git_url)

    def attach_or_clone_repository(
        self,
        name: str,
        git_url: str,
        branch: str | None = None,
    ) -> ProjectSpace:
        clean_name = self._clean_name(name)
        target = (self.root / clean_name).resolve()
        if target.exists() and any(target.iterdir()):
            remote = self._git_remote(target)
            if remote == git_url:
                return self.upsert(clean_name, target, git_remote=git_url, mode="clone", source=git_url)
            raise ProjectSpaceError(
                f"Join target already exists and points somewhere else: {target}. "
                "Use `/bind-repo <name> <path>` for an existing checkout, or pick another project name."
            )
        return self.clone_repository(clean_name, git_url, branch)

    def _load(self) -> dict[str, ProjectSpace]:
        if not self.state_path.exists():
            return {}

        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        return {
            name: ProjectSpace(
                name=name,
                path=Path(item["path"]).resolve(),
                mode=item.get("mode", "local"),
                project_id=item.get("project_id", ""),
                source_kind=item.get("source_kind", "none"),
                visibility=item.get("visibility", "local-only"),
                git_remote=item.get("git_remote"),
                branch=item.get("branch"),
                source=item.get("source"),
            )
            for name, item in raw.items()
        }

    def _save(self, spaces: dict[str, ProjectSpace]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            name: {
                "path": str(space.path),
                "mode": space.mode,
                "project_id": space.project_id,
                "source_kind": space.source_kind,
                "visibility": space.visibility,
                "git_remote": space.git_remote,
                "branch": space.branch,
                "source": space.source,
            }
            for name, space in spaces.items()
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _resolve_project_path(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def _clean_name(self, name: str) -> str:
        clean = name.strip().lower().replace(" ", "-")
        if not clean or any(char in clean for char in "\\/:*?\"<>|"):
            raise ProjectSpaceError("Project space names must be non-empty and path-safe.")
        return clean

    def _git_repo_root(self, path: Path) -> Path:
        if not path.exists():
            raise ProjectSpaceError(f"Repository path does not exist: {path}")
        root = self._maybe_git(["rev-parse", "--show-toplevel"], cwd=path)
        if not root:
            raise ProjectSpaceError(f"Not a git repository: {path}")
        return Path(root).resolve()

    def _stable_project_id(
        self,
        clean_name: str,
        existing: ProjectSpace | None,
        manifest: dict[str, object],
    ) -> str:
        if existing and existing.project_id:
            return existing.project_id
        manifest_id = str(manifest.get("project_id", "")).strip()
        if manifest_id:
            return manifest_id
        return f"{clean_name}-{secrets.token_hex(2)}"

    def _infer_source_kind(
        self,
        git_remote: str | None,
        mode: str,
        manifest: dict[str, object],
        existing: ProjectSpace | None,
    ) -> str:
        stored = str(manifest.get("source_kind", "")).strip() or (existing.source_kind if existing else "")
        if stored:
            return stored
        if git_remote:
            lowered = git_remote.lower()
            if "github.com" in lowered:
                return "github"
            return "git"
        if mode in {"clone", "repo", "worktree"}:
            return "git"
        return "none"

    def _infer_visibility(
        self,
        git_remote: str | None,
        manifest: dict[str, object],
        existing: ProjectSpace | None,
    ) -> str:
        stored = str(manifest.get("visibility", "")).strip() or (existing.visibility if existing else "")
        if stored:
            return stored
        return "private" if git_remote else "local-only"

    def _manifest_path(self, path: Path) -> Path:
        return path / "project_profile.json"

    def _load_manifest(self, path: Path) -> dict[str, object]:
        manifest_path = self._manifest_path(path)
        if not manifest_path.exists():
            return {}
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_manifest(self, space: ProjectSpace) -> None:
        manifest_path = self._manifest_path(space.path)
        payload = {
            "project_id": space.project_id,
            "project_name": space.name,
            "workspace_path": str(space.path),
            "sync_mode": space.mode,
            "source_kind": space.source_kind,
            "visibility": space.visibility,
            "git_remote": space.git_remote or "",
            "branch": space.branch or "",
            "source": space.source or "",
        }
        manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _git_branch(self, path: Path) -> str | None:
        if not self._is_git_workspace_root(path):
            return None
        return self._maybe_git(["branch", "--show-current"], cwd=path)

    def _git_remote(self, path: Path) -> str | None:
        if not self._is_git_workspace_root(path):
            return None
        return self._maybe_git(["remote", "get-url", "origin"], cwd=path)

    def _is_git_workspace_root(self, path: Path) -> bool:
        git_marker = path / ".git"
        return git_marker.exists()

    def _maybe_git(self, args: list[str], cwd: Path) -> str | None:
        try:
            return self._run_git(args, cwd=cwd)
        except ProjectSpaceError:
            return None

    def _run_git(self, args: list[str], cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ProjectSpaceError(detail or f"git {' '.join(args)} failed")
        return result.stdout.strip()


def project_share_pack(space: ProjectSpace) -> str:
    lines = [
        f"Project name: {space.name}",
        f"Workspace mode: {space.mode}",
    ]
    if space.git_remote:
        lines.append(f"Git remote: {space.git_remote}")
    if space.branch:
        lines.append(f"Branch: {space.branch}")
    if space.source:
        lines.append(f"Source: {space.source}")
    if space.git_remote:
        join = f"/clone {space.name} {space.git_remote}"
        if space.branch:
            join += f" {space.branch}"
        lines.append(f"Join command: {join}")
    return "\n".join(lines)


def parse_project_share_pack(text: str) -> dict[str, str]:
    raw = str(text or "").strip()
    if not raw:
        raise ProjectSpaceError("Paste a project share pack or a Git remote URL.")

    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProjectSpaceError(f"Invalid JSON share pack: {exc}") from exc
        return _normalize_share_pack_fields(payload)

    fields: dict[str, str] = {}
    for line in raw.splitlines():
        separator = ":" if ":" in line else "=" if "=" in line else ""
        if not separator:
            continue
        key, value = line.split(separator, 1)
        fields[_normalize_share_pack_key(key)] = value.strip().strip("`")

    if "git_remote" not in fields:
        url_match = re.search(r"(https?://\S+|git@[\w.\-:\/]+)", raw)
        if url_match:
            fields["git_remote"] = url_match.group(1).rstrip(",")

    return _normalize_share_pack_fields(fields)


def project_name_from_remote(git_url: str) -> str:
    tail = git_url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in tail and "/" not in tail:
        tail = git_url.rstrip("/").rsplit(":", 1)[-1]
    tail = tail.removesuffix(".git").strip()
    clean = re.sub(r"[^a-z0-9]+", "-", tail.lower())
    return clean.strip("-") or "shared-project"


def _normalize_share_pack_fields(payload: dict[str, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in payload.items():
        cleaned = _normalize_share_pack_key(key)
        if cleaned:
            normalized[cleaned] = str(value or "").strip()
    remote = normalized.get("git_remote", "")
    if not normalized.get("project_name") and remote:
        normalized["project_name"] = project_name_from_remote(remote)
    return normalized


def _normalize_share_pack_key(key: object) -> str:
    clean = str(key or "").strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "project": "project_name",
        "project name": "project_name",
        "workspace": "project_name",
        "workspace name": "project_name",
        "git remote": "git_remote",
        "remote": "git_remote",
        "repo": "git_remote",
        "repository": "git_remote",
        "repository url": "git_remote",
        "branch": "branch",
        "workspace mode": "mode",
        "mode": "mode",
        "source": "source",
    }
    return aliases.get(clean, clean.replace(" ", "_"))
