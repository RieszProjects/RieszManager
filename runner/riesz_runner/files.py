"""Read-only browsing of project files."""

from __future__ import annotations

import mimetypes
import os
import subprocess
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


def _git_ignored(directory: Path, entries: list[dict[str, Any]]) -> set[str]:
    """Names in `directory` that git ignores (nothing when it is not in a git work tree)."""
    if not entries:
        return set()
    # A trailing slash lets directory-only patterns such as "tmp/" match.
    names = [e["name"] + ("/" if e["dir"] else "") for e in entries]
    try:
        proc = subprocess.run(["git", "check-ignore", "--stdin", "-z"], cwd=directory, input="\0".join(names).encode(),
                              capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode not in (0, 1):  # 128: not a repository
        return set()
    return {n.rstrip("/") for n in proc.stdout.decode(errors="replace").split("\0") if n}


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
    ignored = _git_ignored(directory, entries)
    for e in entries:
        e["ignored"] = e["name"] in ignored
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return entries


# Audio formats browsers can play, with the Content-Type to serve them with.
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".weba": "audio/webm",
}

# Web asset types that Python's mimetypes table can miss or misclassify on minimal Linux installs.
EXTRA_MIME_TYPES = {
    ".cjs": "text/javascript", ".mjs": "text/javascript", ".js": "text/javascript",
    ".webp": "image/webp", ".avif": "image/avif", ".svg": "image/svg+xml",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".wasm": "application/wasm", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
    ".vtt": "text/vtt", ".mp4": "video/mp4", ".webm": "video/webm",
}

_SENSITIVE_DIRS = {".git", ".ssh", ".gnupg", ".aws", ".claude", ".codex", ".config", ".kube", ".docker"}
_SENSITIVE_PREFIXES = ("/etc", "/proc", "/sys", "/root", "/dev", "/var/run")
_SENSITIVE_EXTS = {".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx"}
_SENSITIVE_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}


def audio_type(path: Path) -> str | None:
    return AUDIO_TYPES.get(path.suffix.lower())


def mime_type(path: Path) -> str | None:
    ext = path.suffix.lower()
    return AUDIO_TYPES.get(ext) or EXTRA_MIME_TYPES.get(ext) or mimetypes.guess_type(path.name)[0]


def is_sensitive_path(path: Path) -> bool:
    """True when `path` looks like credentials, keys, git internals, or system files."""
    s = str(path)
    for prefix in _SENSITIVE_PREFIXES:
        if s == prefix or s.startswith(prefix + "/"):
            return True
    if any(part in _SENSITIVE_DIRS for part in path.parts):
        return True
    name = path.name
    if name in _SENSITIVE_NAMES or name.startswith(".env.") or name.startswith(("id_rsa", "id_ed25519")):
        return True
    return path.suffix.lower() in _SENSITIVE_EXTS


def safe_view_asset(raw: str, allowed_roots: list[Path], allow_external: bool = False) -> Path:
    """Resolve an explicit absolute local asset referenced by an HTML preview."""
    if not raw or not raw.startswith("/"):
        raise FileAccessError("an absolute path is needed")
    target = Path(os.path.realpath(raw))
    if not target.is_file():
        raise FileAccessError("not a file")
    if is_sensitive_path(target):
        raise FileAccessError("access to sensitive file is refused")
    if allow_external:
        return target
    for root in allowed_roots:
        root_real = Path(os.path.realpath(root))
        if target == root_real or root_real in target.parents:
            return target
    raise FileAccessError("path is outside allowed asset directories")


def describe_file(root: Path, rel: str, max_bytes: int) -> dict[str, Any]:
    path = safe_join(root, rel)
    if not path.is_file():
        raise FileAccessError("not a file")
    st = path.stat()
    mime, _ = mimetypes.guess_type(path.name)
    info: dict[str, Any] = {"path": rel, "name": path.name, "size": st.st_size, "mtime": st.st_mtime, "mime": mime}
    if audio_type(path):
        info.update(kind="audio", mime=audio_type(path))
        return info
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


def search(root: Path, query: str, limit: int = 40, max_entries: int = 30000) -> list[dict[str, Any]]:
    """Fuzzy-ish path search for @-mentions: ranks basename prefix > basename substring > path substring."""
    q = query.strip().lower()
    root_real = Path(os.path.realpath(root))
    scored: list[tuple[int, str, bool]] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root_real):
        dirnames[:] = [d for d in dirnames if d not in HIDDEN_NAMES and d not in HEAVY_DIRS]
        rel_dir = os.path.relpath(dirpath, root_real)
        rel_dir = "" if rel_dir == "." else rel_dir
        for name, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
            seen += 1
            if seen > max_entries:
                break
            rel = os.path.join(rel_dir, name) if rel_dir else name
            low_name = name.lower()
            low_rel = rel.lower()
            if not q:
                score = 3 + rel.count("/")
            elif low_name.startswith(q):
                score = 0
            elif q in low_name:
                score = 1
            elif q in low_rel:
                score = 2
            else:
                continue
            scored.append((score * 1000 + len(rel), rel, is_dir))
        if seen > max_entries:
            break
    scored.sort()
    return [{"path": rel + ("/" if is_dir else ""), "dir": is_dir} for _, rel, is_dir in scored[:limit]]
