"""Usage limits of the subscriptions other than Claude's (Claude's are usage.py's), shown by the doll.

- ChatGPT (Codex): its 5-hour and weekly windows from `account/rateLimits/read`, taken from a
  running Codex session's latest update when there is one, else from a short-lived app-server.

Each entry: {"id": route, "label", "plan", "windows": [{"label", "kind", "percent", "resetsAt",
"resetText"}], "limited", "error"}, the windows shaped like usage.py's so the web app shows them alike.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from .drivers.codex import CLIENT_INFO
from .drivers.jsonrpc import JsonRpcPeer

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

CACHE_SECONDS = 5 * 60
MIN_REFRESH_SECONDS = 60
LIVE_SECONDS = 10 * 60          # a running session's update is used if it is this recent
KINDS = {300: "session", 1440: "day", 10080: "week"}


def codex_windows(snapshot: dict[str, Any] | None) -> tuple[list[dict[str, Any]], bool]:
    """Codex's rate-limit snapshot as usage windows, and whether a limit has been reached."""
    windows = []
    for key in ("primary", "secondary"):
        w = (snapshot or {}).get(key)
        if not isinstance(w, dict) or w.get("usedPercent") is None:
            continue
        mins = w.get("windowDurationMins")
        kind = KINDS.get(mins, "other")
        label = f"codex {key}" if kind != "other" else (f"{mins // 60} 小时" if mins and mins % 60 == 0 else f"{mins} 分钟")
        windows.append({"label": label, "kind": kind, "percent": float(w["usedPercent"]),
                        "resetsAt": w.get("resetsAt"), "resetText": None})
    return windows, bool((snapshot or {}).get("rateLimitReachedType"))


class Limits:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._view: dict[str, Any] | None = None
        self._fetched = 0.0
        self._lock = asyncio.Lock()
        self._live: tuple[float, dict[str, Any]] | None = None     # the latest update from a running Codex session

    def codex_update(self, snapshot: dict[str, Any] | None) -> None:
        """A running Codex session on the ChatGPT login reported its limits."""
        if isinstance(snapshot, dict):
            self._live = (time.time(), snapshot)

    async def get(self, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            age = time.time() - self._fetched
            if self._view is not None and (age < MIN_REFRESH_SECONDS or (age < CACHE_SECONDS and not force)):
                return self._view
            entries = [e for e in await asyncio.gather(self._codex()) if e is not None]
            self._fetched = time.time()
            self._view = {"limits": entries, "fetchedAt": self._fetched}
            return self._view

    async def _codex(self) -> dict[str, Any] | None:
        login = await self.hub.credentials.subscription("openai", self.hub.bins())
        if login is None:
            return None
        entry: dict[str, Any] = {"id": "openai:subscription", "label": "ChatGPT 订阅", "plan": None, "windows": [],
                                 "limited": False, "error": None}
        try:
            if self._live and time.time() - self._live[0] < LIVE_SECONDS:
                snapshot = self._live[1]
            else:
                snapshot = await self._read_codex()
            entry["windows"], entry["limited"] = codex_windows(snapshot)
            entry["plan"] = (snapshot or {}).get("planType")
        except Exception as exc:  # noqa: BLE001 - shown as a failed check
            entry["error"] = str(exc) or type(exc).__name__
            log.info("codex limits check failed: %s", entry["error"])
        return entry

    async def _read_codex(self) -> dict[str, Any] | None:
        codex = self.hub.harnesses.bin("codex")
        if not codex:
            raise RuntimeError("codex is not installed")
        env = dict(os.environ)
        for var in ("OPENAI_API_KEY", "RUNNER_TOKEN"):
            env.pop(var, None)
        peer = JsonRpcPeer([codex, "app-server"], cwd=str(self.hub.cfg.state_dir), env=env, version=False)
        await peer.start()
        try:
            await peer.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": None}, timeout=60)
            await peer.notify("initialized")
            res = await peer.request("account/rateLimits/read", None, timeout=30)
        finally:
            await peer.stop()
        return (res or {}).get("rateLimits")
