"""Mapping between working directories and "projects".

A project is a direct child directory of the workspace root. Sessions whose
working directory lies inside a project belong to it. Sessions started
elsewhere are grouped by their working directory as *external* projects.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import Config

ROOT_ID = "~root"
EXTERNAL_PREFIX = "ext:"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _unb64(text: str) -> str:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad).decode()


class ProjectResolver:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.workspace_root

    def project_id_for(self, cwd: str | None) -> str | None:
        if not cwd:
            return None
        path = Path(cwd)
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return EXTERNAL_PREFIX + _b64(cwd)
        if not rel.parts:
            return ROOT_ID
        return rel.parts[0]

    def path_for(self, project_id: str) -> Path | None:
        if project_id == ROOT_ID:
            return self.root
        if project_id.startswith(EXTERNAL_PREFIX):
            try:
                return Path(_unb64(project_id[len(EXTERNAL_PREFIX):]))
            except (ValueError, UnicodeDecodeError):
                return None
        if "/" in project_id or project_id in ("", ".", ".."):
            return None
        return self.root / project_id

    def is_external(self, project_id: str) -> bool:
        return project_id.startswith(EXTERNAL_PREFIX)

    def files_allowed(self, project_id: str) -> bool:
        return not self.is_external(project_id) or self.cfg.allow_external_files

    def workspace_projects(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return out
        for child in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            out.append(self.describe(child.name, child))
        return out

    def describe(self, project_id: str, path: Path) -> dict[str, Any]:
        external = self.is_external(project_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        return {
            "id": project_id,
            "name": path.name if project_id != ROOT_ID else (path.name or "/"),
            "path": str(path),
            "external": external,
            "isRoot": project_id == ROOT_ID,
            "exists": path.is_dir(),
            "isGit": (path / ".git").exists(),
            "filesAllowed": self.files_allowed(project_id),
            "mtime": mtime,
        }


def git_branch(path: Path) -> str | None:
    head = path / ".git" / "HEAD"
    try:
        content = head.read_text().strip()
    except OSError:
        return None
    if content.startswith("ref: refs/heads/"):
        return content[len("ref: refs/heads/"):]
    return content[:8] or None


def git_status(path: Path) -> dict[str, Any] | None:
    """Return a compact `git status` summary for a project, or None if unavailable."""
    if not (path / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "-b", "--no-optional-locks"],
            capture_output=True, text=True, timeout=5, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    lines = out.stdout.splitlines()
    branch_line = lines[0] if lines and lines[0].startswith("##") else ""
    changes: dict[str, str] = {}
    for line in lines[1:]:
        if len(line) > 3:
            changes[line[3:].split(" -> ")[-1]] = line[:2].strip() or "?"
    return {"branch": branch_line[3:].split("...")[0] if branch_line else None, "changes": changes}
