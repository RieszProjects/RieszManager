"""Reader sticky notes written by agents (via MCP tools) or the user.

Notes live in `<state dir>/notes.json` so they survive runner restarts and reloads,
and stay pinned in the web app's floating notes window rather than scrolling away
under newer messages or being lost to context compaction.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

COLORS = ("amber", "sage", "slate", "rose")
DEFAULT_COLOR = "amber"
MAX_TITLE_CHARS = 80
MAX_CONTENT_CHARS = 4000
MAX_NOTES = 200


class NoteError(ValueError):
    pass


class Notes:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.path: Path = hub.cfg.state_dir / "notes.json"
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            nid = item.get("id")
            content = item.get("content")
            if isinstance(nid, str) and nid and isinstance(content, str) and content.strip():
                self._items[nid] = {
                    "id": nid,
                    "sessionId": str(item.get("sessionId") or ""),
                    "title": str(item.get("title") or "")[:MAX_TITLE_CHARS],
                    "content": content[:MAX_CONTENT_CHARS],
                    "color": item.get("color") if item.get("color") in COLORS else DEFAULT_COLOR,
                    "createdAt": float(item.get("createdAt") or 0),
                    "updatedAt": float(item.get("updatedAt") or 0),
                }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        ordered = self.list()
        tmp.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self, session_ids: set[str] | None = None) -> list[dict[str, Any]]:
        items = list(self._items.values())
        if session_ids is not None:
            items = [n for n in items if n.get("sessionId") in session_ids]
        items.sort(key=lambda n: (n.get("updatedAt", 0), n.get("createdAt", 0)), reverse=True)
        return [dict(n) for n in items]

    def get(self, note_id: str) -> dict[str, Any] | None:
        item = self._items.get(note_id)
        return dict(item) if item is not None else None

    async def upsert(
        self,
        session_id: str,
        content: str,
        *,
        title: str | None = None,
        note_id: str | None = None,
        color: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(content, str) or not content.strip():
            raise NoteError("便签内容不能为空")
        body = content.strip()
        if len(body) > MAX_CONTENT_CHARS:
            raise NoteError(f"便签内容太长（最多 {MAX_CONTENT_CHARS} 字）")
        clean_title: str | None = None
        if title is not None:
            clean_title = str(title).strip()[:MAX_TITLE_CHARS]
        clean_color: str | None = None
        if color is not None and str(color).strip():
            c = str(color).strip().lower()
            if c not in COLORS:
                raise NoteError("color 只能是 " + "、".join(COLORS))
            clean_color = c

        now = time.time()
        if note_id:
            nid = str(note_id).strip()
            existing = self._items.get(nid)
            if existing is None:
                raise NoteError(f"找不到便签：{nid}")
            updated = dict(existing)
            updated["content"] = body
            if clean_title is not None:
                updated["title"] = clean_title
            if clean_color is not None:
                updated["color"] = clean_color
            updated["updatedAt"] = now
            self._items[nid] = updated
            self._save()
            await self.hub.broadcast_global({"ev": "notes", "action": "update", "note": dict(updated)})
            return dict(updated), False

        if len(self._items) >= MAX_NOTES:
            # Drop the oldest note so agents never hit a dead end.
            oldest = min(self._items.values(), key=lambda n: (n.get("updatedAt", 0), n.get("createdAt", 0)))
            self._items.pop(oldest["id"], None)

        nid = secrets.token_hex(4)
        while nid in self._items:
            nid = secrets.token_hex(4)
        note = {
            "id": nid,
            "sessionId": str(session_id or ""),
            "title": clean_title or "",
            "content": body,
            "color": clean_color or DEFAULT_COLOR,
            "createdAt": now,
            "updatedAt": now,
        }
        self._items[nid] = note
        self._save()
        await self.hub.broadcast_global({"ev": "notes", "action": "create", "note": dict(note)})
        return dict(note), True

    async def update(self, note_id: str, data: dict[str, Any]) -> dict[str, Any]:
        nid = str(note_id).strip()
        existing = self._items.get(nid)
        if existing is None:
            raise NoteError(f"找不到便签：{nid}")
        content = data["content"] if "content" in data else existing["content"]
        title = data["title"] if "title" in data else None
        color = data["color"] if "color" in data else None
        note, _ = await self.upsert(existing["sessionId"], content, title=title, note_id=nid, color=color)
        return note

    async def delete(self, note_id: str) -> dict[str, Any]:
        nid = str(note_id).strip()
        removed = self._items.pop(nid, None)
        if removed is None:
            raise NoteError(f"找不到便签：{nid}")
        self._save()
        await self.hub.broadcast_global({"ev": "notes", "action": "delete", "id": nid})
        return dict(removed)

    async def clear(self, session_ids: set[str] | None = None) -> int:
        if session_ids is None:
            count = len(self._items)
            self._items.clear()
        else:
            drop = [nid for nid, n in self._items.items() if n.get("sessionId") in session_ids]
            for nid in drop:
                self._items.pop(nid, None)
            count = len(drop)
        if count:
            self._save()
            await self.hub.broadcast_global({"ev": "notes", "action": "clear"})
        return count
