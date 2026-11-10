"""Scheduled wake-ups: the runner sends a session a message on a timetable.

Claude Code's own timers (/loop's ScheduleWakeup, CronCreate) live inside the
session's claude process and are gone when it stops: after a reboot, a crash, or
when the runner stops an idle process. These schedules live in the runner
instead (<state dir>/schedules.json), so they survive all of that: at the right
time the runner delivers the message the usual way, starting the session if it
is not running and queueing behind its current turn if it is busy.

A schedule belongs to one session and has either
- `times` ("HH:MM", in `tz`) on `days` (0 = Monday; none = every day), or
- `every` (minutes, at least 5) counted from its last run,
plus an optional random delay (`jitter`, minutes), and `catchUp`: whether a run
missed by more than a few minutes (the server was down) still happens, once.
`{now}` in the prompt becomes the time it is sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import time
import uuid as uuidlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

CHECK_INTERVAL = 15.0
GRACE_SECONDS = 600          # a run later than this counts as missed
MIN_EVERY_MINUTES = 5
MAX_JITTER_MINUTES = 720
MAX_PROMPT_CHARS = 20000
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FIELDS = ("sessionId", "name", "prompt", "times", "days", "every", "tz", "jitter", "catchUp", "enabled")


class ScheduleError(ValueError):
    pass


def system_timezone() -> str:
    """The server's IANA time zone name (falls back to UTC). /etc/localtime is what the system uses;
    /etc/timezone can be a stale leftover."""
    candidates = []
    try:
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            candidates.append(target.split("/zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return "UTC"


def validate(data: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """A complete, checked schedule from `data` (over `current` when updating)."""
    s = dict(current or {})
    for key in FIELDS:
        if key in data:
            s[key] = data[key]
    if not isinstance(s.get("sessionId"), str) or not UUID_RE.match(s["sessionId"]):
        raise ScheduleError("sessionId must be a session id")
    prompt = s.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ScheduleError("prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ScheduleError("prompt is too long")
    s["name"] = str(s.get("name") or "").strip()[:80]
    times = s.get("times") or []
    every = s.get("every")
    if times and every:
        raise ScheduleError("give either times or every, not both")
    if times:
        if not isinstance(times, list) or not all(isinstance(t, str) and TIME_RE.match(t.strip()) for t in times):
            raise ScheduleError('times must be a list of "HH:MM"')
        s["times"] = sorted({f"{int(t.split(':')[0]):02d}:{t.split(':')[1].strip()}" for t in times})
        s["every"] = None
    elif every:
        try:
            every = int(every)
        except (TypeError, ValueError):
            raise ScheduleError("every must be a number of minutes") from None
        if every < MIN_EVERY_MINUTES:
            raise ScheduleError(f"every must be at least {MIN_EVERY_MINUTES} minutes")
        s["every"], s["times"] = every, []
    else:
        raise ScheduleError("give times or every")
    days = s.get("days") or []
    if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        raise ScheduleError("days must be a list of 0 (Monday) to 6 (Sunday)")
    s["days"] = sorted(set(days)) if len(set(days)) < 7 else []
    tz = s.get("tz") or system_timezone()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"unknown time zone: {tz}") from None
    s["tz"] = tz
    try:
        jitter = int(s.get("jitter") or 0)
    except (TypeError, ValueError):
        raise ScheduleError("jitter must be a number of minutes") from None
    if not 0 <= jitter <= MAX_JITTER_MINUTES:
        raise ScheduleError(f"jitter must be between 0 and {MAX_JITTER_MINUTES} minutes")
    s["jitter"] = jitter
    s["catchUp"] = bool(s.get("catchUp"))
    s["enabled"] = bool(s.get("enabled", True))
    return s


def next_base(s: dict[str, Any], after: float) -> float | None:
    """The next scheduled moment (before any random delay) strictly after `after`."""
    if s.get("every"):
        last = s.get("lastBase") or s.get("createdAt") or after
        base = last + s["every"] * 60
        if base <= after:  # missed several: continue from now rather than firing for each
            base = after + s["every"] * 60 if base + GRACE_SECONDS < after else after + 1
        return base
    tz = ZoneInfo(s["tz"])
    days = set(s.get("days") or range(7))
    start = datetime.fromtimestamp(after, tz)
    for offset in range(0, 9):
        day = (start + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        for t in s["times"]:
            hh, mm = (int(x) for x in t.split(":"))
            moment = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).timestamp()
            if moment > after:
                return moment
    return None


def plan(s: dict[str, Any], after: float) -> None:
    """Set the schedule's next run (with its random delay) after `after`."""
    base = next_base(s, after)
    s["nextBase"] = base
    s["next"] = None if base is None else base + random.uniform(0, s.get("jitter", 0) * 60)


class Scheduler:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.path: Path = hub.cfg.state_dir / "schedules.json"
        self.items: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- storage ---------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("cannot read %s: %s", self.path, exc)
            return
        for s in data.get("schedules", []) if isinstance(data, dict) else []:
            if isinstance(s, dict) and isinstance(s.get("id"), str):
                self.items[s["id"]] = s

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schedules": list(self.items.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    async def _changed(self) -> None:
        self._save()
        await self.hub.broadcast_global({"ev": "schedules"})

    # ---- API ---------------------------------------------------------------------

    def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        out = [s for s in self.items.values() if session_id is None or s["sessionId"] == session_id]
        return sorted(out, key=lambda s: (s.get("next") or float("inf")))

    def get(self, sid: str) -> dict[str, Any]:
        s = self.items.get(sid)
        if s is None:
            raise ScheduleError("unknown schedule")
        return s

    async def create(self, data: dict[str, Any], created_by: str | None = None) -> dict[str, Any]:
        s = validate({"enabled": True, **data})
        s.update(id=secrets.token_hex(6), createdAt=time.time(), createdBy=created_by, last=None,
                 lastBase=None, lastResult=None, lastUuid=None)
        plan(s, time.time())
        self.items[s["id"]] = s
        await self._changed()
        log.info("schedule %s added for %s: %s", s["id"], s["sessionId"][:8], describe(s))
        return s

    async def update(self, sid: str, data: dict[str, Any]) -> dict[str, Any]:
        current = self.get(sid)
        s = validate(data, current)
        timing = any(k in data for k in ("times", "days", "every", "tz", "jitter")) or (s["enabled"] and not current.get("enabled"))
        self.items[sid] = s
        if timing:
            plan(s, time.time())
        await self._changed()
        return s

    async def delete(self, sid: str) -> None:
        self.get(sid)
        del self.items[sid]
        await self._changed()

    async def run_now(self, sid: str) -> dict[str, Any]:
        s = self.get(sid)
        await self._fire(s, manual=True)
        await self._changed()
        return s

    # ---- running -------------------------------------------------------------------

    async def loop(self) -> None:
        await asyncio.sleep(5.0)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("scheduler tick failed")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        if self.hub.reload_pending or self.hub.reload_ready.is_set():
            return
        now = time.time()
        changed = False
        for s in list(self.items.values()):
            if not s.get("enabled"):
                continue
            if s.get("next") is None:
                plan(s, now)
                changed = True
                continue
            if s["next"] > now:
                continue
            late = now - s["next"]
            if late > GRACE_SECONDS + s.get("jitter", 0) * 60 and not s.get("catchUp"):
                s["lastResult"] = "missed"
                log.info("schedule %s for %s missed its run %.0f min ago", s["id"], s["sessionId"][:8], late / 60)
                s["lastBase"] = s.get("nextBase")
                plan(s, now)
            else:
                await self._fire(s)
            changed = True
        if changed:
            await self._changed()

    async def _fire(self, s: dict[str, Any], manual: bool = False) -> None:
        now = time.time()
        if not manual:
            s["lastBase"] = s.get("nextBase") or now
        sid = s["sessionId"]
        proc = self.hub.get_hosted(sid)
        if proc is not None and proc.alive and s.get("lastUuid") in proc.queue:
            s["lastResult"] = "skipped: the previous message is still queued"
        else:
            text = s["prompt"].replace("{now}", datetime.now(ZoneInfo(s["tz"])).strftime("%Y-%m-%d %H:%M %Z"))
            message_uuid = str(uuidlib.uuid4())
            try:
                result = await self.hub.delivery.send(sid, text, [], message_uuid)
                s["lastUuid"] = message_uuid
                s["lastResult"] = "held" if result.get("held") else "sent"
                log.info("schedule %s woke %s", s["id"], sid[:8])
            except Exception as exc:  # noqa: BLE001 - recorded and shown to the user
                s["lastResult"] = f"failed: {exc}"[:300]
                log.warning("schedule %s could not wake %s: %s", s["id"], sid[:8], exc)
        s["last"] = now
        if not manual:
            plan(s, now)


DAY_NAMES = "一二三四五六日"


def describe(s: dict[str, Any]) -> str:
    if s.get("every"):
        rule = f"每 {s['every']} 分钟"
    else:
        days = s.get("days") or []
        when = "每天" if not days else "每周" + "、".join(DAY_NAMES[d] for d in days)
        rule = f"{when} {', '.join(s['times'])}"
    if s.get("jitter"):
        rule += f"（随机推迟至多 {s['jitter']} 分钟）"
    return rule
