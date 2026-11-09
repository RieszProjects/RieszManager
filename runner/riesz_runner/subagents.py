"""Subagent transcripts of a session.

Claude Code runs a subagent inside the session's own process and keeps its
conversation next to the session's transcript:

    <projects>/<project dir>/<session id>/subagents/agent-<id>.jsonl      the subagent's records
    <projects>/<project dir>/<session id>/subagents/agent-<id>.meta.json  toolUseId, description, agentType, ...

`toolUseId` is the Agent tool call in the session that started it, which is how
the web app finds the subagent of a tool card. Views are built incrementally and
cached like session views; a subagent transcript can be tens of megabytes, so
reading happens in a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .store import LoadedView, SessionStore, _Cursor, _read_increment
from .transcript import TranscriptView

AGENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VIEW_CACHE_SIZE = 6


class SubagentStore:
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._views: OrderedDict[str, LoadedView] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def _dir(self, session_id: str) -> Path | None:
        entry = self.store.get(session_id)
        return entry.path.with_suffix("") / "subagents" if entry else None

    def list(self, session_id: str) -> list[dict[str, Any]]:
        folder = self._dir(session_id)
        if folder is None or not folder.is_dir():
            return []
        out = []
        for meta_path in folder.glob("agent-*.meta.json"):
            agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            transcript = folder / f"agent-{agent_id}.jsonl"
            try:
                st = transcript.stat()
            except OSError:
                continue
            out.append({
                "agentId": agent_id,
                "toolUseId": meta.get("toolUseId"),
                "description": meta.get("description") or "",
                "agentType": meta.get("agentType") or "",
                "background": meta.get("requestShape") == "background",
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        out.sort(key=lambda a: a["mtime"])
        return out

    async def items(self, session_id: str, agent_id: str) -> tuple[list[dict[str, Any]], float] | None:
        """All display items of one subagent, and the transcript's mtime."""
        folder = self._dir(session_id)
        if folder is None or not AGENT_ID.match(agent_id):
            return None
        path = folder / f"agent-{agent_id}.jsonl"
        if not path.is_file():
            return None
        key = str(path)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._catch_up, key, path)

    def _catch_up(self, key: str, path: Path) -> tuple[list[dict[str, Any]], float]:
        st = path.stat()
        loaded = self._views.get(key)
        if loaded is None or st.st_ino != loaded.cursor.inode or st.st_size < loaded.cursor.offset:
            loaded = LoadedView(view=TranscriptView(sidechain=True), cursor=_Cursor(0, st.st_ino))
            self._views[key] = loaded
        self._views.move_to_end(key)
        while len(self._views) > VIEW_CACHE_SIZE:
            old, _ = self._views.popitem(last=False)
            self._locks.pop(old, None)
        if st.st_size > loaded.cursor.offset:
            lines, loaded.cursor.offset = _read_increment(path, loaded.cursor.offset)
            for off, raw in lines:
                loaded.view.feed_line(raw, off)
        loaded.view.take_dirty()
        return loaded.view.items, st.st_mtime
