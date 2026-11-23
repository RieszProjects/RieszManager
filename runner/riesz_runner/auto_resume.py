"""Picking a session up again by itself after a turn failed for a reason that passes.

A turn can end before its work is done because the subscription's quota ran out,
the account ran out of money or has a bill due, the provider rate-limited it, the
network or the provider's servers failed, or the harness process died. The
process or driver that saw it puts a `failure` into the turn's result (see
`failure()`); this module remembers the session (<state dir>/auto_resume.json, so
it survives restarts) and, once the session can run again, sends it a message
telling it to carry on:

- when the provider said when the quota resets: a minute after that;
- otherwise after a growing delay (2, 5, 15, 30 minutes, then every hour), for at most two days;
- a harness process that died mid-turn: at most three times.

A failed attempt is turned away before the model runs, so it costs nothing; it
just schedules the next one. Another message that goes through (a schedule, a
message from another session) means the session can run: it is picked up right
away. The user's own message, or stopping the session, means the user has taken
over: the wait is dropped, and so is it when the user cancels it in the web app.
Subagent sessions are left alone: whoever started them has the failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid as uuidlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

CAUSES = {
    "quota": "额度用完了",
    "balance": "余额不足或欠费",
    "rate_limit": "请求被限流",
    "network": "网络出错",
    "server": "服务端出错",
    "exit": "进程意外退出",
}
BACKOFF = (120, 300, 900, 1800, 3600)       # seconds before attempt 1, 2, ...; then hourly
AFTER_RESET = 60
GIVE_UP_AFTER = 2 * 24 * 3600
EXIT_ATTEMPTS = 3
CHECK_INTERVAL = 15.0
BUSY_RETRY = 60
STUCK_SENT_GRACE = 60.0

NETWORK_RE = re.compile(r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|socket hang up|fetch failed|can't reach|"
                        r"network|connection (lost|reset|refused|closed|error)|timed? ?out|stream (disconnected|closed)", re.I)
_TEXT_CAUSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("balance", re.compile(r"insufficient[ _]balance|余额不足|payment (is )?(past due|required)|billing|out of credits|"
                           r"credit balance is too low|\b402\b", re.I)),
    ("quota", re.compile(r"quota|resource[ _]exhausted|usage limit|exhausted your|hit your .*limit|reached your .*limit", re.I)),
    ("rate_limit", re.compile(r"rate[ _-]?limit|too many requests|\b429\b", re.I)),
    ("server", re.compile(r"overloaded|internal server error|service unavailable|bad gateway|gateway time-?out|"
                          r"\b50[0234]\b|server error|unavailable", re.I)),
    ("network", NETWORK_RE),
)


def failure(cause: str, message: str = "", resets_at: float | None = None) -> dict[str, Any]:
    """What a result carries when its turn failed for a reason that passes."""
    out: dict[str, Any] = {"cause": cause, "message": " ".join(str(message or "").split())[:300]}
    if resets_at:
        out["resetsAt"] = float(resets_at)
    return out


def failure_from_text(text: str, resets_at: float | None = None) -> dict[str, Any] | None:
    """For harnesses that report a failure only as a message (ACP agents): guess the cause from its words."""
    for cause, pattern in _TEXT_CAUSES:
        if pattern.search(text or ""):
            return failure(cause, text, resets_at)
    return None


# ---- each harness's failures ------------------------------------------------------------

_CLAUDE_ERRORS = {"billing_error": "balance", "oauth_org_not_allowed": "balance", "server_error": "server"}


def claude_failure(result: dict[str, Any], error_message: dict[str, Any] | None,
                   rejected: dict[str, Any] | None) -> dict[str, Any] | None:
    """Claude Code (stream-json): the `result` message of a failed turn, the synthetic assistant message
    carrying the API error, and the last rate_limit_event that said "rejected"."""
    if not result.get("is_error"):
        return None
    err = error_message or {}
    kind = err.get("error")
    text = result.get("result") if isinstance(result.get("result"), str) else ""
    status = result.get("api_error_status") or err.get("api_error_status")
    info = (err.get("api_error_params") or {}).get("rate_limit_info") or {}
    resets = info.get("resetsAt") if info.get("status") == "rejected" else None
    if not resets and rejected and rejected.get("status") == "rejected":
        resets = rejected.get("resetsAt")
    if kind == "rate_limit" or status == 429:
        return failure("quota" if resets else "rate_limit", text, resets)
    if kind in _CLAUDE_ERRORS:
        cause = _CLAUDE_ERRORS[kind]
        if cause == "server" and NETWORK_RE.search(text):
            cause = "network"
        return failure(cause, text)
    if isinstance(status, int) and status >= 500:
        return failure("server", text)
    if kind == "unknown" or (kind is None and result.get("terminal_reason") == "api_error"):
        return failure_from_text(text)
    return None


_CODEX_ERRORS = {"rateLimitExceeded": "rate_limit", "serverOverloaded": "server", "internalServerError": "server",
                 "httpConnectionFailed": "network", "responseStreamConnectionFailed": "network",
                 "responseStreamDisconnected": "network", "responseTooManyFailedAttempts": "network"}


def codex_failure(error: dict[str, Any] | None, rate_limits: dict[str, Any] | None) -> dict[str, Any] | None:
    """Codex: the `error` of a failed turn, and the latest rate-limit snapshot (when the used-up window resets)."""
    error = error or {}
    info = error.get("codexErrorInfo")
    key = info if isinstance(info, str) else next(iter(info), None) if isinstance(info, dict) else None
    text = error.get("message") or ""
    if key == "usageLimitExceeded":
        windows = [w for w in ((rate_limits or {}).get("primary"), (rate_limits or {}).get("secondary")) if isinstance(w, dict)]
        full = [w["resetsAt"] for w in windows if (w.get("usedPercent") or 0) >= 100 and w.get("resetsAt")]
        return failure("quota", text, max(full) if full else None)
    if key in _CODEX_ERRORS:
        return failure(_CODEX_ERRORS[key], text)
    if key is None:
        return failure_from_text(text)
    return None


_DSH_ERRORS = {"QUOTA": "balance", "ACCOUNT_QUOTA": "quota", "RATE_LIMIT": "rate_limit", "SERVER": "server",
               "EMPTY_RESPONSE": "server", "TIMEOUT": "network", "TRANSPORT": "network", "STREAM_CLOSED": "network"}


def dsh_failure(reason: dict[str, Any] | None) -> dict[str, Any] | None:
    """dsh: the reason of a turn/end; errors from the model carry a provider-neutral code."""
    reason = reason or {}
    if reason.get("kind") != "error":
        return None
    err = reason.get("error") or {}
    cause = _DSH_ERRORS.get(str(err.get("code") or ""))
    if cause is None:
        return None
    retry = err.get("providerRetryAfterMs")
    return failure(cause, err.get("message") or "", time.time() + retry / 1000 if isinstance(retry, (int, float)) else None)


def describe(entry: dict[str, Any]) -> str:
    return CAUSES.get(entry.get("cause") or "", "出错了")


class AutoResume:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.path: Path = hub.cfg.state_dir / "auto_resume.json"
        self.items: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- storage -----------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("cannot read %s: %s", self.path, exc)
            return
        changed = False
        now = time.time()
        for entry in data.get("sessions", []) if isinstance(data, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
                sid = entry["sessionId"]
                self.items[sid] = entry
                if entry.get("sentAt") is not None and entry.get("nextAt") is None:
                    # The runner restarted before the prior resume attempt reported a result.
                    self._reconcile_stuck(sid, entry, now, delay=CHECK_INTERVAL)
                    changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": list(self.items.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot write %s: %s", self.path, exc)

    async def _changed(self, sid: str) -> None:
        self._save()
        if self.hub.harnesses.has(sid) or sid in self.hub.store.entries or sid in self.hub.hosted:
            await self.hub.broadcast_session(sid, {"ev": "state", "state": self.hub.session_state(sid)})
            await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.hub.session_summary(sid)], "removed": []})

    # ---- what the web app shows --------------------------------------------------------

    def view(self, sid: str) -> dict[str, Any] | None:
        entry = self.items.get(sid)
        if entry is None:
            return None
        return {"cause": entry["cause"], "label": describe(entry), "message": entry.get("message") or "",
                "nextAt": entry.get("nextAt"), "sentAt": entry.get("sentAt"), "since": entry.get("since"),
                "attempts": entry.get("attempts", 0), "resetsAt": entry.get("resetsAt")}

    # ---- what happens to the session --------------------------------------------------

    def _watched(self, sid: str) -> bool:
        pref = self.hub.prefs.get(sid) or {}
        if pref.get("subagentOf") or pref.get("handedOffTo"):
            return False
        rec = self.hub.harnesses.records.get(sid)
        if rec and rec.get("handedOffTo"):
            return False
        return True

    async def on_result(self, sid: str, result: dict[str, Any]) -> None:
        if not self._watched(sid):
            return
        fail = result.get("failure")
        entry = self.items.get(sid)
        if fail:
            await self._failed(sid, fail)
        elif entry is None:
            return
        elif entry.get("sentAt") is None and result.get("subtype") == "success" and not result.get("isError"):
            entry["nextAt"] = time.time()               # someone else's message went through: it can run again
            await self._changed(sid)
        else:
            log.info("auto-resume of %s done (%s)", sid[:8], result.get("subtype"))
            del self.items[sid]
            await self._changed(sid)

    async def on_exit(self, sid: str, *, during_turn: bool, requested: bool) -> None:
        if requested or not during_turn or self.hub.closing or self.hub.reload_ready.is_set() or not self._watched(sid):
            return
        await self._failed(sid, failure("exit", "进程在这一轮中途退出"))

    async def _failed(self, sid: str, fail: dict[str, Any]) -> None:
        now = time.time()
        entry = self.items.get(sid) or {"sessionId": sid, "since": now, "attempts": 0}
        resets_at = fail.get("resetsAt")
        if not resets_at and fail.get("cause") == "quota":
            rec = self.hub.harnesses.records.get(sid)
            route = rec.get("api") if rec else (self.hub.prefs.get(sid) or {}).get("api")
            resets_at = self.hub.limits.reset_time(route)
        entry.update(cause=fail.get("cause") or "server", message=fail.get("message") or "", resetsAt=resets_at, sentAt=None)
        entry.pop("sentUuid", None)
        if now - entry["since"] > GIVE_UP_AFTER or (entry["cause"] == "exit" and entry["attempts"] >= EXIT_ATTEMPTS):
            log.info("auto-resume of %s given up after %d attempts", sid[:8], entry["attempts"])
            self.items.pop(sid, None)
            await self._changed(sid)
            return
        entry["nextAt"] = self._next(entry, now)
        self.items[sid] = entry
        log.info("%s stopped (%s: %s); picking it up again at %s", sid[:8], entry["cause"], entry["message"][:80],
                 datetime.fromtimestamp(entry["nextAt"]).strftime("%m-%d %H:%M"))
        await self._changed(sid)

    @staticmethod
    def _next(entry: dict[str, Any], now: float) -> float:
        resets = entry.get("resetsAt")
        if resets and resets > now:
            return resets + AFTER_RESET
        return now + BACKOFF[min(entry.get("attempts", 0), len(BACKOFF) - 1)]

    async def taken_over(self, sid: str) -> bool:
        """The user sent the session a message or stopped it: no more waiting."""
        if self.items.pop(sid, None) is None:
            return False
        log.info("auto-resume of %s dropped: the user took over", sid[:8])
        await self._changed(sid)
        return True

    # ---- running ---------------------------------------------------------------------------

    async def loop(self) -> None:
        await asyncio.sleep(5.0)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - keep it alive
                log.exception("auto-resume tick failed")
            await asyncio.sleep(CHECK_INTERVAL)

    def _reconcile_stuck(self, sid: str, entry: dict[str, Any], now: float, *, delay: float | None = None) -> None:
        """Recover an entry left in `sentAt != None, nextAt == None` after a restart or dropped result."""
        entry["sentAt"] = None
        entry.pop("sentUuid", None)
        if now - float(entry.get("since") or now) > GIVE_UP_AFTER or (
            entry.get("cause") == "exit" and entry.get("attempts", 0) >= EXIT_ATTEMPTS
        ):
            log.info("auto-resume of %s given up during reconcile after %d attempts", sid[:8], entry.get("attempts", 0))
            self.items.pop(sid, None)
            return
        entry["nextAt"] = (now + delay) if delay is not None else self._next(entry, now)

    async def _tick(self) -> None:
        if self.hub.reload_ready.is_set():
            return
        now = time.time()
        for sid, entry in list(self.items.items()):
            if not self._watched(sid):
                self.items.pop(sid, None)
                self._save()
                continue
            if entry.get("sentAt") is not None and entry.get("nextAt") is None:
                if (
                    now - float(entry["sentAt"]) >= STUCK_SENT_GRACE
                    and not self.hub.session_working(sid)
                    and not self.hub.message_queued(sid, entry.get("sentUuid"))
                ):
                    log.info("auto-resume of %s stuck in sent state while idle; rescheduling", sid[:8])
                    self._reconcile_stuck(sid, entry, now)
                    await self._changed(sid)
                continue
            if entry.get("nextAt") is None or entry["nextAt"] > now:
                continue
            if self.hub.session_working(sid):
                entry["nextAt"] = now + BUSY_RETRY      # its current turn decides (its result arrives here)
                continue
            await self._fire(sid, entry)

    async def _fire(self, sid: str, entry: dict[str, Any]) -> None:
        message_uuid = str(uuidlib.uuid4())
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["sentAt"] = time.time()
        entry["sentUuid"] = message_uuid
        entry["nextAt"] = None
        try:
            await self.hub.delivery.send(sid, resume_text(entry), [], message_uuid)
            log.info("auto-resume: picked %s up again (attempt %d)", sid[:8], entry["attempts"])
        except Exception as exc:  # noqa: BLE001 - tried again later
            log.warning("auto-resume: could not message %s: %s", sid[:8], exc)
            entry["sentAt"] = None
            entry.pop("sentUuid", None)
            entry["nextAt"] = self._next(entry, time.time())
        await self._changed(sid)


def resume_text(entry: dict[str, Any]) -> str:
    if entry.get("cause") == "exit":
        return ("[Riesz Manager 自动续跑] 上一轮做到一半，运行你的进程意外退出了，当时在跑的命令和后台任务也都停了。"
                "请先确认做到了哪一步，然后接着做完。")
    detail = f"（{entry['message']}）" if entry.get("message") else ""
    return (f"[Riesz Manager 自动续跑] 上一轮因为{describe(entry)}没做完{detail}，现在可以继续了。"
            "请从中断的地方接着做；如果其实已经做完了，简短说一下结果就行。")
