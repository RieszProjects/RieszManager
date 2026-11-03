"""Storage for files attached to prompts, and composition of user message content."""

from __future__ import annotations

import base64
import json
import re
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _sanitize(name: str) -> str:
    name = Path(name).name or "file"
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._") or "file"
    return name[:120]


class UploadStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, filename: str, mime: str | None, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("file is too large")
        upload_id = uuidlib.uuid4().hex
        folder = self.root / upload_id
        folder.mkdir(parents=True, exist_ok=False)
        safe = _sanitize(filename)
        path = folder / safe
        path.write_bytes(data)
        meta = {"id": upload_id, "name": safe, "originalName": filename, "mime": mime or "application/octet-stream",
                "size": len(data), "path": str(path), "createdAt": time.time()}
        (folder / ".meta.json").write_text(json.dumps(meta))
        return meta

    def get(self, upload_id: str) -> dict[str, Any] | None:
        if not _ID_RE.match(upload_id or ""):
            return None
        meta_file = self.root / upload_id / ".meta.json"
        try:
            return json.loads(meta_file.read_text())
        except (OSError, ValueError):
            return None

    def cleanup(self, max_age_days: int = 30) -> int:
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        if not self.root.is_dir():
            return 0
        for folder in self.root.iterdir():
            meta = self.get(folder.name)
            if meta and meta.get("createdAt", 0) < cutoff:
                for f in folder.iterdir():
                    f.unlink(missing_ok=True)
                folder.rmdir()
                removed += 1
        return removed


def compose_content(text: str, attachments: list[dict[str, Any]]) -> tuple[Any, str]:
    """Build Messages-API user content from prompt text and stored uploads.

    Images are inlined as base64 image blocks (like pasting into the CLI);
    other files are referenced with `@path` mentions, which Claude Code expands
    into file attachments itself. Returns (content, preview_text).
    """
    image_blocks: list[dict[str, Any]] = []
    mentions: list[str] = []
    for meta in attachments:
        path = Path(meta["path"])
        if meta.get("mime") in IMAGE_TYPES and meta.get("size", 0) <= MAX_INLINE_IMAGE_BYTES:
            image_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": meta["mime"],
                           "data": base64.b64encode(path.read_bytes()).decode()},
            })
        else:
            mentions.append(f"@{path}")
    full_text = text
    if mentions:
        full_text = (text.rstrip() + "\n\n" if text.strip() else "") + " ".join(mentions)
    preview = full_text if full_text else f"[{len(image_blocks)} image(s)]"
    if not image_blocks:
        return full_text, preview
    blocks: list[dict[str, Any]] = list(image_blocks)
    if full_text:
        blocks.append({"type": "text", "text": full_text})
    return blocks, preview
