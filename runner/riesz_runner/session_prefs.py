"""Per-session settings the runner keeps on disk, so they survive runner restarts.

- effort and permission mode, to carry over when a session is resumed: Claude
  Code does not keep a session's effort (a resume falls back to the model's
  default), and in stream-json mode it does not record permission-mode changes
  in the transcript either;
- keepAlive: the user asked for the session's claude process to stay running
  (not stopped when idle, started again after a runner restart);
- startedBy: the session that asked for this one with `rieszctl session new`.
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
        """Store non-empty fields (effort, permissionMode, startedBy); writes only when something changed."""
        current = self._data.get(session_id, {})
        updates = {k: v for k, v in fields.items() if v}
        if all(current.get(k) == v for k, v in updates.items()):
            return
        self._data[session_id] = {**current, **updates, "time": time.time()}
        self._prune()
        self._save()

    def keep_alive(self, session_id: str) -> bool:
        return bool(self._data.get(session_id, {}).get("keepAlive"))

    def keep_alive_ids(self) -> list[str]:
        return [sid for sid, v in self._data.items() if v.get("keepAlive")]

    def set_keep_alive(self, session_id: str, on: bool) -> None:
        current = dict(self._data.get(session_id, {}))
        if bool(current.get("keepAlive")) == on:
            return
        if on:
            current["keepAlive"] = True
        else:
            current.pop("keepAlive", None)
        current["time"] = time.time()
        self._data[session_id] = current
        self._prune()
        self._save()

    def _prune(self) -> None:
        """Forget the oldest entries beyond MAX_ENTRIES (never one marked keepAlive)."""
        if len(self._data) <= MAX_ENTRIES:
            return
        candidates = sorted((k for k, v in self._data.items() if not v.get("keepAlive")),
                            key=lambda k: self._data[k].get("time", 0))
        for key in candidates[: len(self._data) - MAX_ENTRIES]:
            del self._data[key]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        os.replace(tmp, self.path)
