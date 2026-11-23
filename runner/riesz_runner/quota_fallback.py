"""Automatic DeepSeek API fallback for Claude Code subscription quota exhaustion.

When a hosted Claude Code session running on the user's `anthropic:subscription`
hits its quota limit (`failure.cause == "quota"`) and a DeepSeek API key is
configured, this module switches that session in-place to `deepseek:key`
(`deepseek-flash`) and sends a continuation prompt so work carries on without
waiting for the subscription window to reset.

State is persisted to `<state dir>/quota_fallback.json` so fallback sessions
survive runner restarts. Once Claude subscription quota recovers (verified via
`UsageMonitor`, or after `resetsAt + AFTER_RESET` if `/usage` is unavailable),
each fallback session is automatically switched back to `anthropic:subscription`
and its remembered Claude model as soon as it is idle (never interrupting a
running turn). Manual route changes cancel automatic restore.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid as uuidlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

SOURCE_ROUTE = "anthropic:subscription"
FALLBACK_ROUTE = "deepseek:key"
FALLBACK_MODEL = "deepseek-flash"
AFTER_RESET = 60.0
CHECK_INTERVAL = 30.0
PROBE_INTERVAL = 600.0
IDLE_DRAIN_SECONDS = 2.0

FALLBACK_RESUME_TEXT = (
    "[Riesz Manager 自动切换] Claude 订阅额度已用尽，已自动切换至 DeepSeek API 继续执行；"
    "额度恢复后会自动切回。请直接从刚才中断的地方继续完成上一轮任务。"
)


def _claude_model_or_none(model: Any) -> str | None:
    if not isinstance(model, str) or not model or model == "<synthetic>" or model.startswith("deepseek"):
        return None
    return model


class ClaudeQuotaFallback:
    """Manages Claude subscription -> DeepSeek API fallback and automatic restore."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.path: Path = hub.cfg.state_dir / "quota_fallback.json"
        self.items: dict[str, dict[str, Any]] = {}
        self._switching: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_probe = 0.0
        self._probe_soon = False
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
        for entry in data.get("sessions", []) if isinstance(data, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
                self.items[entry["sessionId"]] = entry

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": list(self.items.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot write %s: %s", self.path, exc)

    def _lock(self, sid: str) -> asyncio.Lock:
        lock = self._locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sid] = lock
        return lock

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _changed(self, sid: str) -> None:
        self._save()
        if not self.hub.harnesses.has(sid) and (sid in self.hub.store.entries or sid in self.hub.hosted):
            await self.hub.broadcast_session(sid, {"ev": "state", "state": self.hub.session_state(sid)})
            await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.hub.session_summary(sid)], "removed": []})

    # ---- state inspection --------------------------------------------------------

    def has_in_flight(self) -> bool:
        return bool(self._switching)

    def view(self, sid: str) -> dict[str, Any] | None:
        entry = self.items.get(sid)
        if entry is None:
            return None
        return {
            "fromRoute": entry.get("fromRoute") or SOURCE_ROUTE,
            "toRoute": entry.get("toRoute") or FALLBACK_ROUTE,
            "originalModel": entry.get("originalModel"),
            "fallbackModel": entry.get("fallbackModel") or FALLBACK_MODEL,
            "resetsAt": entry.get("resetsAt"),
            "switchedAt": entry.get("switchedAt"),
            "switching": sid in self._switching or entry.get("switchedAt") is None,
            "restorePending": bool(entry.get("restorePending")),
        }

    # ---- event hooks -------------------------------------------------------------

    def on_rate_limit(self, info: Any) -> None:
        """A non-rejected Claude subscription rate-limit event hints that quota may be back."""
        if not self.items or not isinstance(info, dict):
            return
        status = str(info.get("status") or "")
        if status and status != "rejected":
            self._probe_soon = True

    async def on_manual_api_change(self, sid: str, route: str) -> None:
        """Respect an explicit user route choice by dropping automatic restore for this session."""
        if sid not in self.items:
            return
        self.items.pop(sid, None)
        log.info("quota fallback for %s cleared: user manually selected %s", sid[:8], route)
        self._save()

    async def on_result(self, sid: str, result: dict[str, Any]) -> bool:
        """Intercept Claude subscription quota failures and trigger fallback; also finish deferred restores.

        Returns True when the failure was intercepted for DeepSeek fallback (caller should skip auto_resume
        and subagent failure notification for this turn).
        """
        if self.hub.harnesses.has(sid):
            return False

        # If this session is on fallback and quota already recovered while it was mid-turn, restore now.
        existing = self.items.get(sid)
        if existing is not None:
            if existing.get("restorePending") and sid not in self._switching and not self.hub.session_working(sid):
                self._switching.add(sid)
                self._spawn(self._restore(sid))
            return False

        fail = result.get("failure")
        if not isinstance(fail, dict) or fail.get("cause") != "quota":
            return False
        pref = self.hub.prefs.get(sid) or {}
        if pref.get("handedOffTo"):
            return False
        current_route = pref.get("api") or SOURCE_ROUTE
        if current_route != SOURCE_ROUTE:
            return False
        if not self.hub.credentials.configured("deepseek"):
            return False
        if sid in self._switching or self.hub.closing or self.hub.reload_ready.is_set():
            return False

        proc = self.hub.get_hosted(sid)
        entry_meta = self.hub.store.entries.get(sid)
        orig_model = _claude_model_or_none(proc.model if proc is not None else None) or _claude_model_or_none(
            entry_meta.scanner.meta.model if entry_meta is not None else None
        )
        resets_at = fail.get("resetsAt") or self.hub.usage.reset_time()
        self.items[sid] = {
            "sessionId": sid,
            "fromRoute": SOURCE_ROUTE,
            "toRoute": FALLBACK_ROUTE,
            "originalModel": orig_model,
            "fallbackModel": FALLBACK_MODEL,
            "resetsAt": float(resets_at) if resets_at else None,
            "switchedAt": None,
            "resumePending": True,
            "restorePending": False,
            "reason": str(fail.get("message") or "")[:300],
            "fail": dict(fail),
        }
        self._save()
        self._switching.add(sid)
        # Run detached from proc._read_stdout so hub.set_api can cleanly stop and restart ClaudeProcess.
        self._spawn(self._activate(sid))
        return True

    # ---- switching & restoring ---------------------------------------------------

    async def _activate(self, sid: str) -> None:
        try:
            async with self._lock(sid):
                entry = self.items.get(sid)
                if entry is None:
                    return
                # Wait briefly for any finishing turn teardown; if background jobs are running, defer to _tick().
                deadline = time.monotonic() + IDLE_DRAIN_SECONDS
                while self.hub.session_working(sid) and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                if self.hub.session_working(sid):
                    log.info("quota fallback for %s deferred while background work finishes", sid[:8])
                    await self._changed(sid)
                    return

                fail = entry.get("fail") or {"cause": "quota", "message": entry.get("reason") or ""}
                try:
                    await self.hub.set_api(sid, FALLBACK_ROUTE, model=FALLBACK_MODEL, _internal=True)
                    entry["switchedAt"] = time.time()
                    if not entry.get("resetsAt"):
                        with contextlib.suppress(Exception):
                            await self.hub.usage.get(force=True)
                        resets = self.hub.usage.reset_time()
                        if resets:
                            entry["resetsAt"] = float(resets)
                    entry.pop("fail", None)
                    await self._changed(sid)
                    when = (
                        f"（预计 {datetime.fromtimestamp(entry['resetsAt']).strftime('%m-%d %H:%M')} 恢复后自动切回）"
                        if entry.get("resetsAt") else "（额度恢复后自动切回）"
                    )
                    log.info("quota fallback: switched %s to %s (%s)%s", sid[:8], FALLBACK_ROUTE, FALLBACK_MODEL, when)
                    await self.hub.broadcast_session(
                        sid,
                        {"ev": "notice", "level": "info", "text": f"Claude 订阅额度已用尽，已自动切换至 DeepSeek API 继续执行{when}"},
                    )
                    if entry.get("resumePending"):
                        assert self.hub.delivery is not None
                        await self.hub.delivery.send(
                            sid, FALLBACK_RESUME_TEXT, [], str(uuidlib.uuid4()), model=FALLBACK_MODEL,
                        )
                        entry["resumePending"] = False
                        self._save()
                except Exception as exc:  # noqa: BLE001 - fall back to normal auto-resume so the session isn't stranded
                    log.warning("quota fallback for %s failed: %s; delegating to auto-resume", sid[:8], exc)
                    self.items.pop(sid, None)
                    await self._changed(sid)
                    await self.hub.auto_resume.on_result(sid, {"isError": True, "failure": fail})
        finally:
            self._switching.discard(sid)

    async def _check_quota_recovered(self, now: float, resets_at: float | None) -> tuple[bool, float | None]:
        """Probe Claude subscription `/usage` (zero tokens) to verify whether quota is available again."""
        self._last_probe = now
        self._probe_soon = False
        data: dict[str, Any] = {}
        try:
            data = await self.hub.usage.get(force=True)
        except Exception as exc:  # noqa: BLE001
            log.info("quota recovery probe failed: %s", exc)

        limited = data.get("limited") if isinstance(data, dict) else None
        if isinstance(limited, dict):
            lim_reset = limited.get("resetsAt")
            if not lim_reset or float(lim_reset) > now:
                return False, float(lim_reset) if lim_reset else resets_at

        windows = data.get("windows") if isinstance(data, dict) else None
        if isinstance(windows, list) and windows:
            full = [w for w in windows if isinstance(w, dict) and float(w.get("percent") or 0) >= 100.0]
            if full:
                resets = [float(w["resetsAt"]) for w in full if w.get("resetsAt")]
                return False, max(resets) if resets else resets_at
            return True, None

        # `/usage` probe failed or returned no windows: fall back to the known Anthropic reset timestamp.
        if resets_at is not None and now >= resets_at + AFTER_RESET:
            return True, None
        return False, resets_at

    async def _restore(self, sid: str) -> None:
        try:
            async with self._lock(sid):
                entry = self.items.get(sid)
                if entry is None:
                    return
                pref = self.hub.prefs.get(sid) or {}
                if pref.get("handedOffTo") or (pref.get("api") or SOURCE_ROUTE) != FALLBACK_ROUTE:
                    self.items.pop(sid, None)
                    self._save()
                    return
                if self.hub.session_working(sid):
                    entry["restorePending"] = True
                    await self._changed(sid)
                    return

                target_route = entry.get("fromRoute") or SOURCE_ROUTE
                orig_model = _claude_model_or_none(entry.get("originalModel"))
                try:
                    await self.hub.set_api(sid, target_route, model=orig_model, _internal=True)
                except Exception as exc:  # noqa: BLE001 - retry on next tick
                    log.warning("quota fallback restore for %s failed: %s", sid[:8], exc)
                    entry["restorePending"] = True
                    await self._changed(sid)
                    return

                self.items.pop(sid, None)
                await self._changed(sid)
                # If the fallback route itself hit balance/rate-limit and scheduled auto-resume, let it resume now.
                ar_entry = self.hub.auto_resume.items.get(sid)
                if ar_entry is not None and ar_entry.get("sentAt") is None:
                    ar_entry["nextAt"] = time.time()
                    await self.hub.auto_resume._changed(sid)
                log.info("quota fallback: restored %s back to %s", sid[:8], target_route)
                await self.hub.broadcast_session(
                    sid,
                    {"ev": "notice", "level": "info", "text": "Claude 订阅额度已恢复，已自动切回 Claude 订阅"},
                )
        finally:
            self._switching.discard(sid)

    # ---- periodic watcher --------------------------------------------------------

    async def loop(self) -> None:
        await asyncio.sleep(5.0)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - keep watcher alive
                log.exception("quota fallback tick failed")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        if self.hub.reload_ready.is_set() or self.hub.closing or not self.items:
            return
        now = time.time()

        # 1. Clean up stale entries and retry any activation that was deferred while background tasks ran.
        active: list[tuple[str, dict[str, Any]]] = []
        for sid, entry in list(self.items.items()):
            pref = self.hub.prefs.get(sid) or {}
            if self.hub.harnesses.has(sid) or pref.get("handedOffTo"):
                self.items.pop(sid, None)
                self._save()
                continue
            if entry.get("switchedAt") is None:
                if sid not in self._switching and not self.hub.session_working(sid):
                    self._switching.add(sid)
                    self._spawn(self._activate(sid))
                continue
            if (pref.get("api") or SOURCE_ROUTE) != FALLBACK_ROUTE:
                self.items.pop(sid, None)
                self._save()
                continue
            active.append((sid, entry))

        if not active:
            return

        # 2. If any entry is already marked ready to restore, restore once idle.
        need_probe: list[tuple[str, dict[str, Any]]] = []
        for sid, entry in active:
            if entry.get("restorePending"):
                if sid not in self._switching and not self.hub.session_working(sid):
                    self._switching.add(sid)
                    self._spawn(self._restore(sid))
            else:
                need_probe.append((sid, entry))

        if not need_probe:
            return

        # 3. Decide whether to probe Claude subscription quota now (shared across all active fallback sessions).
        due_by_reset = any(
            entry.get("resetsAt") is not None and now >= float(entry["resetsAt"]) + AFTER_RESET
            for _, entry in need_probe
        )
        due_by_interval = (now - self._last_probe) >= PROBE_INTERVAL
        if not (due_by_reset or due_by_interval or self._probe_soon):
            return

        resets_candidates = [float(e["resetsAt"]) for _, e in need_probe if e.get("resetsAt") is not None]
        recovered, latest_reset = await self._check_quota_recovered(now, max(resets_candidates) if resets_candidates else None)

        if not recovered:
            if latest_reset is not None:
                for sid, entry in need_probe:
                    if entry.get("resetsAt") != latest_reset:
                        entry["resetsAt"] = latest_reset
                        await self._changed(sid)
            return

        for sid, entry in need_probe:
            entry["restorePending"] = True
            if sid not in self._switching and not self.hub.session_working(sid):
                self._switching.add(sid)
                self._spawn(self._restore(sid))
            else:
                await self._changed(sid)
