"""Prompt history (for arrow-key recall in the composer).

Merges Claude Code's own `~/.claude/history.jsonl` with prompts sent through
this runner, which are kept in the runner's state directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MAX_SCAN_BYTES = 4 * 1024 * 1024


def _tail_lines(path: Path, max_bytes: int = MAX_SCAN_BYTES) -> list[bytes]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            return fh.read().splitlines()
    except OSError:
        return []


class PromptHistory:
    def __init__(self, claude_history: Path, own_history: Path) -> None:
        self.claude_history = claude_history
        self.own_history = own_history

    def record(self, text: str, project: str | None, session_id: str) -> None:
        if not text.strip():
            return
        self.own_history.parent.mkdir(parents=True, exist_ok=True)
        entry = {"display": text, "timestamp": int(time.time() * 1000), "project": project, "sessionId": session_id}
        with self.own_history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def query(self, project: str | None, limit: int = 100) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in (self.claude_history, self.own_history):
            for raw in _tail_lines(path):
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                text = rec.get("display")
                if not isinstance(text, str) or not text.strip():
                    continue
                if project and rec.get("project") != project:
                    continue
                entries.append({"text": text, "ts": rec.get("timestamp") or 0, "sessionId": rec.get("sessionId")})
        entries.sort(key=lambda e: e["ts"], reverse=True)
        seen: set[str] = set()
        unique = []
        for e in entries:
            if e["text"] in seen:
                continue
            seen.add(e["text"])
            unique.append(e)
            if len(unique) >= limit:
                break
        return unique
