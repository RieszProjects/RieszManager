"""Usage limits of the subscriptions other than Claude's (Claude's are usage.py's), shown by the doll.

- ChatGPT (Codex): its 5-hour and weekly windows from `account/rateLimits/read`, taken from a
  running Codex session's latest update when there is one, else from a short-lived app-server.
- Google (Antigravity): `agy -p /quota`, which prints each model group's windows (no model is called).

Each entry: {"id": route, "label", "plan", "windows": [{"label", "kind", "percent", "resetsAt",
"resetText"}], "limited", "error"}, the windows shaped like usage.py's so the web app shows them alike.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .credentials import _child_env, _output
from .drivers.codex import CLIENT_INFO
from .drivers.jsonrpc import JsonRpcPeer

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

CACHE_SECONDS = 5 * 60
MIN_REFRESH_SECONDS = 60
LIVE_SECONDS = 10 * 60          # a running session's update is used if it is this recent
KINDS = {300: "session", 1440: "day", 10080: "week"}
AGY_KINDS = {"five hour": "session", "daily": "day", "weekly": "week"}
AGY_GROUPS = {"gemini models": "Gemini 模型", "claude and gpt models": "Claude 和 GPT 模型"}


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


def agy_windows(out: str) -> list[dict[str, Any]]:
    """`agy -p /quota` lines ("Gemini Models<TAB>Weekly Limit Remaining<TAB>100%<TAB>2026-11-27T19:46:39Z") as windows."""
    windows = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) != 4 or not parts[2].endswith("%"):
            continue
        group, name, remaining, reset = parts
        try:
            left = float(remaining[:-1])
        except ValueError:
            continue
        try:
            resets_at: float | None = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
        except ValueError:
            resets_at = None
        kind = next((k for key, k in AGY_KINDS.items() if name.lower().startswith(key)), "other")
        scope = AGY_GROUPS.get(group.lower(), group)
        # "(scope)" is shown after the window's name by the web app; an unknown window keeps its own name.
        label = f"{name} ({scope})" if kind != "other" else f"{name}（{scope}）"
        windows.append({"label": label, "kind": kind, "percent": max(0.0, 100.0 - left), "resetsAt": resets_at,
                        "resetText": None if resets_at else reset})
    return windows


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
            entries = [e for e in await asyncio.gather(self._codex(), self._antigravity()) if e is not None]
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

    async def _antigravity(self) -> dict[str, Any] | None:
        login = await self.hub.credentials.subscription("gemini", self.hub.bins())
        if login is None:
            return None
        entry: dict[str, Any] = {"id": "gemini:subscription", "label": "Google 订阅", "plan": None, "windows": [],
                                 "limited": False, "error": None}
        try:
            agy = self.hub.harnesses.bin("antigravity")
            if not agy:
                raise RuntimeError("agy is not installed")
            entry["windows"] = agy_windows(await _output([agy, "-p", "/quota"]))
            if not entry["windows"]:
                raise RuntimeError("agy /quota printed no quota")
            entry["limited"] = any(w["percent"] >= 100 for w in entry["windows"])
        except Exception as exc:  # noqa: BLE001 - shown as a failed check
            entry["error"] = str(exc) or type(exc).__name__
            log.info("antigravity quota check failed: %s", entry["error"])
        return entry

    async def _read_codex(self) -> dict[str, Any] | None:
        codex = self.hub.harnesses.bin("codex")
        if not codex:
            raise RuntimeError("codex is not installed")
        env = _child_env()
        # codex is a Node script: its own directory (nvm's) holds the node it needs, which the service's PATH lacks.
        env["PATH"] = os.path.dirname(codex) + os.pathsep + env.get("PATH", "")
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
