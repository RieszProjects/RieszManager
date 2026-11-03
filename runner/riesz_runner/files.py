"""Read-only browsing of project files."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

HIDDEN_NAMES = {".git"}
HEAVY_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache", "target"}

LANGUAGE_BY_EXT = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "tsx", ".json": "json", ".jsonl": "json", ".md": "markdown",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".sh": "bash", ".bash": "bash",
    ".zsh": "bash", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".sql": "sql", ".xml": "xml",
    ".svg": "xml", ".vue": "xml", ".swift": "swift", ".lua": "lua", ".r": "r", ".tex": "latex",
    ".dockerfile": "dockerfile", ".conf": "nginx", ".env": "bash", ".txt": "plaintext",
}
SPECIAL_NAMES = {"Dockerfile": "dockerfile", "Makefile": "makefile", ".gitignore": "bash", ".env": "bash"}


class FileAccessError(Exception):
    pass


def safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` inside `root`, refusing anything that escapes it (including via symlinks)."""
    root_real = Path(os.path.realpath(root))
    target = Path(os.path.realpath(root_real / rel.lstrip("/")))
    if target != root_real and root_real not in target.parents:
        raise FileAccessError("path is outside the project")
    return target


def list_dir(root: Path, rel: str) -> list[dict[str, Any]]:
    directory = safe_join(root, rel)
    if not directory.is_dir():
        raise FileAccessError("not a directory")
    entries: list[dict[str, Any]] = []
    try:
        children = list(os.scandir(directory))
    except OSError as exc:
        raise FileAccessError(str(exc)) from exc
    for child in children:
        if child.name in HIDDEN_NAMES:
            continue
        try:
            is_dir = child.is_dir()
            st = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": os.path.join(rel, child.name).lstrip("/"),
            "dir": is_dir,
            "size": None if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "heavy": is_dir and child.name in HEAVY_DIRS,
            "symlink": child.is_symlink(),
        })
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return entries


def describe_file(root: Path, rel: str, max_bytes: int) -> dict[str, Any]:
    path = safe_join(root, rel)
    if not path.is_file():
        raise FileAccessError("not a file")
    st = path.stat()
    mime, _ = mimetypes.guess_type(path.name)
    info: dict[str, Any] = {"path": rel, "name": path.name, "size": st.st_size, "mtime": st.st_mtime, "mime": mime}
    if mime and (mime.startswith("image/") or mime in ("application/pdf",)) and not path.suffix == ".svg":
        info["kind"] = "image" if mime.startswith("image/") else "pdf"
        return info
    with path.open("rb") as fh:
        head = fh.read(min(st.st_size, max_bytes))
    if b"\x00" in head[:8192]:
        info["kind"] = "binary"
        return info
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 4 and st.st_size > max_bytes:
            text = head[: exc.start].decode("utf-8", "replace")
        else:
            text = head.decode("utf-8", "replace")
            info["encodingIssues"] = True
    info.update(
        kind="text",
        content=text,
        truncated=st.st_size > max_bytes,
        language=SPECIAL_NAMES.get(path.name) or LANGUAGE_BY_EXT.get(path.suffix.lower(), "plaintext"),
    )
    return info


def raw_path(root: Path, rel: str) -> Path:
    path = safe_join(root, rel)
    if not path.is_file():
        raise FileAccessError("not a file")
    return path
