"""Per-session settings to carry over when a session is resumed.

Claude Code does not keep a session's effort (a resume falls back to the
model's default), and in stream-json mode it does not record permission-mode
changes in the transcript either. The runner therefore remembers the last
effort and permission mode it saw for each session, on disk so that they
survive runner restarts.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

MAX_ENTRIES = 5000


class SessionPrefs:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._data.get(session_id)

    def remember(self, session_id: str, **fields: str | None) -> None:
        """Store non-empty fields (effort, permissionMode); writes only when something changed."""
        current = self._data.get(session_id, {})
        updates = {k: v for k, v in fields.items() if v}
        if all(current.get(k) == v for k, v in updates.items()):
            return
        self._data[session_id] = {**current, **updates, "time": time.time()}
        if len(self._data) > MAX_ENTRIES:
            oldest = sorted(self._data, key=lambda k: self._data[k].get("time", 0))[: len(self._data) - MAX_ENTRIES]
            for key in oldest:
                del self._data[key]
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        os.replace(tmp, self.path)
