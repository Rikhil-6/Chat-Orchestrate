from __future__ import annotations

import json
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
    ) -> ProjectSpace:
        clean_name = self._clean_name(name)
        resolved_path = self._resolve_project_path(path)
        resolved_path.mkdir(parents=True, exist_ok=True)

        branch = self._git_branch(resolved_path)
        space = ProjectSpace(
            name=clean_name,
            path=resolved_path,
            mode=mode,
            git_remote=git_remote or self._git_remote(resolved_path),
            branch=branch,
            source=source,
        )

        spaces = self._load()
        spaces[clean_name] = space
        self._save(spaces)
        return space

    def create_worktree(
        self,
        name: str,
        repository_path: str | Path,
        branch: str,
        base_ref: str = "HEAD",
    ) -> ProjectSpace:
        clean_name = self._clean_name(name)
        repo = Path(repository_path).expanduser().resolve()
        if not (repo / ".git").exists():
            raise ProjectSpaceError(f"Not a git repository: {repo}")

        target = (self.root / clean_name).resolve()
        if target.exists() and any(target.iterdir()):
            raise ProjectSpaceError(f"Worktree target is not empty: {target}")

        self._run_git(["worktree", "add", "-b", branch, str(target), base_ref], cwd=repo)
        return self.upsert(clean_name, target, mode="worktree", source=str(repo))

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

    def _load(self) -> dict[str, ProjectSpace]:
        if not self.state_path.exists():
            return {}

        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        return {
            name: ProjectSpace(
                name=name,
                path=Path(item["path"]).resolve(),
                mode=item.get("mode", "local"),
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

    def _git_branch(self, path: Path) -> str | None:
        return self._maybe_git(["branch", "--show-current"], cwd=path)

    def _git_remote(self, path: Path) -> str | None:
        return self._maybe_git(["remote", "get-url", "origin"], cwd=path)

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
