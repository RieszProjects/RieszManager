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
import os
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
        self._known: dict[str, tuple[float, list[str]]] = {}
        self._metas: dict[Path, tuple[float, dict[str, Any]]] = {}

    def _transcripts(self, session_id: str) -> tuple[Path, list[str]] | None:
        """The session's subagent transcripts (those `list` returns), cached by folder mtime."""
        folder = self._dir(session_id)
        if folder is None:
            return None
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            return None
        cached = self._known.get(session_id)
        if cached and cached[0] == mtime:
            return folder, cached[1]
        try:
            names = {e.name for e in os.scandir(folder)}
        except OSError:
            return None
        found = [name[:-len(".meta.json")] + ".jsonl" for name in names
                 if name.startswith("agent-") and name.endswith(".meta.json")
                 and name[:-len(".meta.json")] + ".jsonl" in names]
        self._known[session_id] = (mtime, found)
        return folder, found

    def _meta(self, path: Path) -> dict[str, Any]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._metas.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        self._metas[path] = (mtime, meta)
        return meta

    def unfinished(self, session_id: str, since: float) -> list[str]:
        """Agent tool calls of subagents not marked finished that wrote after `since` (unix seconds):
        the ones running in a session whose process this runner does not drive."""
        known = self._transcripts(session_id)
        if not known:
            return []
        folder, names = known
        out = []
        for name in names:
            meta = self._meta(folder / (name[:-len(".jsonl")] + ".meta.json"))
            if meta.get("finished") or not meta.get("toolUseId"):
                continue
            try:
                if (folder / name).stat().st_mtime >= since:
                    out.append(meta["toolUseId"])
            except OSError:
                continue
        return out

    def activity(self, session_id: str) -> tuple[int, float | None]:
        """How many subagents the session has started, and when one of them last wrote anything."""
        known = self._transcripts(session_id)
        if not known or not known[1]:
            return 0, None
        folder, names = known
        last = None
        for name in names:
            try:
                last = max(last or 0.0, (folder / name).stat().st_mtime)
            except OSError:
                continue
        return len(names), last

    def _dir(self, session_id: str) -> Path | None:
        entry = self.store.get(session_id)
        return entry.path.with_suffix("") / "subagents" if entry else None

    def list(self, session_id: str) -> list[dict[str, Any]]:
        known = self._transcripts(session_id)
        if not known:
            return []
        folder, names = known
        out = []
        for name in names:
            agent_id = name[len("agent-"):-len(".jsonl")]
            meta = self._meta(folder / f"agent-{agent_id}.meta.json")
            try:
                st = (folder / name).stat()
            except OSError:
                continue
            out.append({
                "agentId": agent_id,
                "toolUseId": meta.get("toolUseId"),
                "description": meta.get("description") or "",
                "agentType": meta.get("agentType") or "",
                "background": meta.get("requestShape") == "background",
                "finished": bool(meta.get("finished")),
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
