"""Plan usage of the Claude account on this machine (the subscription limit windows).

The numbers come from Claude Code's own `/usage` command, run as a one-off
`claude -p /usage`. It is a local command: it makes no model request, costs no
tokens, and with `--no-session-persistence` leaves no transcript behind. Its
text output lists one line per limit window, for example

    Current session: 23% used · resets 5pm (America/New_York)
    Current week (all models): 6% used · resets Nov 10, 3am (America/New_York)

Results are cached; hosted sessions also report rate-limit events, which mark
the cache stale so the next read refreshes it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .process import ClaudeProcess

log = logging.getLogger(__name__)

CACHE_SECONDS = 120
MIN_REFRESH_SECONDS = 15
PROBE_TIMEOUT = 45

_LINE_RE = re.compile(
    r"^\s*(?P<label>[^:\n]{2,80}):\s*(?P<pct>\d{1,3}(?:\.\d+)?)%\s*used"
    r"(?:\s*[·•-]\s*resets\s+(?P<reset>.+?))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RESET_RE = re.compile(
    r"^(?:(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s*\((?P<tz>[^)]+)\))?$",
    re.IGNORECASE,
)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _window_kind(label: str) -> str:
    low = label.lower()
    if "session" in low:
        return "session"
    if "week" in low:
        return "week"
    if "day" in low:
        return "day"
    return "other"


def parse_reset(text: str, now: datetime | None = None) -> float | None:
    """Turn "Nov 10, 3am (America/New_York)" or "5pm (UTC)" into a Unix time."""
    m = _RESET_RE.match(text.strip())
    if not m:
        return None
    tz = None
    if m.group("tz"):
        try:
            tz = ZoneInfo(m.group("tz").strip())
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    current = now or datetime.now(tz)
    if tz is not None and current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    hour = int(m.group("hour")) % 12 + (12 if m.group("ampm").lower() == "pm" else 0)
    minute = int(m.group("min") or 0)
    if m.group("mon"):
        month = _MONTHS.get(m.group("mon")[:3].lower())
        if not month:
            return None
        try:
            target = current.replace(month=month, day=int(m.group("day")), hour=hour, minute=minute,
                                     second=0, microsecond=0)
        except ValueError:
            return None
        if target < current - timedelta(days=1):
            target = target.replace(year=target.year + 1)
    else:
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
    return target.timestamp()


def parse_usage(text: str) -> dict[str, Any]:
    """Extract the limit windows from `/usage` output."""
    windows = []
    for m in _LINE_RE.finditer(text):
        label = m.group("label").strip()
        reset_text = (m.group("reset") or "").strip() or None
        windows.append({
            "label": label,
            "kind": _window_kind(label),
            "percent": float(m.group("pct")),
            "resetsAt": parse_reset(reset_text) if reset_text else None,
            "resetText": reset_text,
        })
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    plan = "subscription" if "subscription" in first.lower() else ("api" if "api" in first.lower() else None)
    return {"windows": windows, "plan": plan}


class UsageMonitor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._data: dict[str, Any] | None = None
        self._fetched = 0.0
        self._stale = False
        self._lock = asyncio.Lock()
        self.limited: dict[str, Any] | None = None

    def note_rate_limit(self, info: Any) -> None:
        """Called for every rate-limit event of a hosted session."""
        if not isinstance(info, dict):
            return
        self._stale = True
        status = str(info.get("status") or "")
        if status == "rejected":
            self.limited = {"resetsAt": info.get("resetsAt"), "type": info.get("rateLimitType"), "at": time.time()}
        elif status:
            self.limited = None

    async def get(self, force: bool = False) -> dict[str, Any]:
        age = time.time() - self._fetched
        if self._data is not None and age < CACHE_SECONDS and not self._stale and not force:
            return self._view()
        if self._data is not None and age < MIN_REFRESH_SECONDS:
            return self._view()
        async with self._lock:
            if self._data is None or time.time() - self._fetched >= MIN_REFRESH_SECONDS:
                try:
                    self._data = await self._probe()
                    self._stale = False
                except Exception as exc:  # keep serving the last good value
                    log.warning("usage probe failed: %s", exc)
                    if self._data is None:
                        self._data = {"windows": [], "plan": None, "error": str(exc)[:200]}
                self._fetched = time.time()
        return self._view()

    def _view(self) -> dict[str, Any]:
        data = dict(self._data or {})
        data["fetchedAt"] = self._fetched
        if self.limited and (not self.limited.get("resetsAt") or self.limited["resetsAt"] > time.time()):
            data["limited"] = self.limited
        return data

    async def _probe(self) -> dict[str, Any]:
        cwd = self.cfg.state_dir / "probe"
        cwd.mkdir(parents=True, exist_ok=True)
        args = [self.cfg.claude_bin, "-p", "/usage", "--output-format", "json",
                "--no-session-persistence", "--strict-mcp-config"]
        if self.cfg.login_shell:
            shell = os.environ.get("SHELL") or "/bin/bash"
            args = [shell, "-lc", 'exec "$0" "$@"', *args]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=ClaudeProcess._child_env(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("claude /usage timed out") from None
        text = ""
        for line in out.decode(errors="replace").splitlines()[::-1]:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and isinstance(msg.get("result"), str):
                text = msg["result"]
                break
        if not text:
            raise RuntimeError("claude /usage returned no output")
        return parse_usage(text)
