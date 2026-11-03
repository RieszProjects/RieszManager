"""Discovery of Claude Code processes that are currently running on this machine.

Every Claude Code process (interactive TUI, background agent, or SDK/stream-json
child) registers itself in `~/.claude/sessions/<pid>.json`. Stale files are
filtered out by checking that the pid is alive.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LiveProcess:
    pid: int
    session_id: str
    cwd: str | None
    kind: str | None
    entrypoint: str | None
    status: str | None
    name: str | None
    started_at: int | None
    updated_at: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "kind": self.kind,
            "entrypoint": self.entrypoint,
            "status": self.status,
            "name": self.name,
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
        }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def scan_live(sessions_dir: Path) -> dict[str, LiveProcess]:
    """Return live processes keyed by session id (newest process wins)."""
    result: dict[str, LiveProcess] = {}
    if not sessions_dir.is_dir():
        return result
    for file in sessions_dir.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            pid = int(data.get("pid"))
        except (OSError, ValueError, TypeError):
            continue
        sid = data.get("sessionId")
        if not sid or not _pid_alive(pid):
            continue
        proc = LiveProcess(
            pid=pid,
            session_id=sid,
            cwd=data.get("cwd"),
            kind=data.get("kind"),
            entrypoint=data.get("entrypoint"),
            status=data.get("status"),
            name=data.get("name"),
            started_at=data.get("startedAt"),
            updated_at=data.get("updatedAt"),
        )
        current = result.get(sid)
        if current is None or (proc.started_at or 0) > (current.started_at or 0):
            result[sid] = proc
    return result
