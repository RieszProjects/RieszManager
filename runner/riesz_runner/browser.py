"""Dedicated, persistent server-side Chrome for agents (via chrome-devtools-mcp)
and for the user (via the web app's interactive CDP screencast viewer).

Chrome runs with its own user-data-dir under `<state_dir>/browser-profile` and
remote debugging bound strictly to 127.0.0.1. Cookies and site logins live in
that profile: the user logs in directly through the web viewer (CDP input events
go straight to Chrome and never enter any model context), and every harness uses
the same browser through the mounted `browser` MCP server (`chrome-devtools-mcp`).
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import json
import logging
import os
import re
import signal
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web

from .config import Config
from .drivers.base import ToolServer
from .harnesses import find_bin

log = logging.getLogger(__name__)

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800
WATCHDOG_INTERVAL = 15.0
# A browser that goes down again within this long after starting is crash-looping: restarts back off.
CRASH_WINDOW = 120.0
_CHROME_BINS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def find_chrome(configured: str = "") -> str | None:
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    for name in _CHROME_BINS:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_mcp_bin(configured: str = "") -> str | None:
    return find_bin("chrome-devtools-mcp", configured or None)


def mcp_server(cfg: Config) -> ToolServer | None:
    """The `browser` MCP server (chrome-devtools-mcp) pointing at the managed Chrome, if installed."""
    if not cfg.browser_enabled or not find_chrome(cfg.browser_bin):
        return None
    mcp_bin = find_mcp_bin(cfg.browser_mcp_bin)
    if not mcp_bin:
        return None
    # Invoke through the sibling node binary so harnesses with a minimal PATH still launch it.
    sibling_node = Path(mcp_bin).parent / "node"
    node = str(sibling_node) if os.access(sibling_node, os.X_OK) else shutil.which("node")
    common = (
        "--browserUrl", f"http://127.0.0.1:{cfg.browser_port}",
        "--usageStatistics=false",
        "--categoryPerformance=false",
        "--categoryMemory=false",
        "--workspace", str(cfg.workspace_root),
    )
    if node:
        command, args = node, (mcp_bin, *common)
    else:
        command, args = mcp_bin, common
    return ToolServer(name="browser", command=command, args=args, config_dir=str(cfg.state_dir / "tools"))


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# Plain http: loopback, IP addresses, LAN names (nas.local, router.lan) and bare host:port (my-server:8080).
_LOCAL_HOST_RE = re.compile(
    r"^(localhost|(\d{1,3}\.){3}\d{1,3}|\[[0-9a-fA-F:]+\]|([a-zA-Z0-9-]+\.)*[a-zA-Z0-9-]+\.(local|lan|internal|home\.arpa))"
    r"(:\d+)?([/?#].*)?$|^[a-zA-Z0-9-]+:\d+([/?#].*)?$", re.I)
_DOMAIN_RE = re.compile(r"^([a-zA-Z0-9-]+\.)+([a-zA-Z]{2,}|xn--[a-zA-Z0-9-]+)(:\d+)?([/?#].*)?$")


def normalize_url(raw: str) -> str:
    """Chrome-like omnibox normalization: navigate URLs/domains directly, Google search plain text."""
    url = raw.strip()
    if not url:
        return "about:blank"
    if re_scheme(url):
        return url
    if any(ch.isspace() for ch in url):
        return "https://www.google.com/search?q=" + urllib.parse.quote_plus(url)
    if _LOCAL_HOST_RE.match(url):
        return "http://" + url
    if _DOMAIN_RE.match(url):
        return "https://" + url
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(url)


def re_scheme(url: str) -> bool:
    return bool(_SCHEME_RE.match(url)) or url.lower().startswith(
        ("about:", "chrome:", "data:", "javascript:", "view-source:", "mailto:", "tel:"))


def _pick_free_display(start: int = 98, end: int = 199) -> str | None:
    for n in range(start, end):
        if not Path(f"/tmp/.X{n}-lock").exists() and not Path(f"/tmp/.X11-unix/X{n}").exists():
            return f":{n}"
    return None


class ServerBrowser:
    """Owns the persistent server Chrome (+ optional private Xvfb) and its CDP HTTP/WS helpers."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.port = cfg.browser_port
        self.profile_dir = cfg.browser_profile_dir
        self._lock = asyncio.Lock()
        self._chrome: asyncio.subprocess.Process | None = None
        self._xvfb: asyncio.subprocess.Process | None = None
        self._display: str | None = None
        self._last_error: str | None = None
        self._user_stopped = False
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._stderr_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.browser_enabled

    @property
    def chrome_bin(self) -> str | None:
        return find_chrome(self.cfg.browser_bin)

    @property
    def available(self) -> bool:
        return self.enabled and self.chrome_bin is not None

    @property
    def mcp_ready(self) -> bool:
        return self.available and find_mcp_bin(self.cfg.browser_mcp_bin) is not None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _cdp_ready(self) -> bool:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as http:
                async with http.get(f"{self.base_url}/json/version") as resp:
                    return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Continuously drain Chrome stderr so its pipe buffer never fills and blocks the browser."""
        stream = proc.stderr
        if stream is None:
            return
        pending = b""
        while True:
            try:
                # Chunks rather than lines: readline() gives up on a line longer than its buffer, and a
                # pipe nobody reads any more fills up until Chrome blocks writing to it.
                chunk = await stream.read(65536)
            except Exception:  # noqa: BLE001
                break
            if not chunk:
                break
            *lines, pending = (pending + chunk).split(b"\n")
            pending = pending[-4096:]
            for line in lines:
                text = line[:4096].decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_tail.append(text)

    async def start_background(self) -> None:
        """Start the persistent server Chrome and watch it, auto-restarting if it crashes unexpectedly."""
        failures = 0
        crashes = 0            # starts that came up but went down again soon after
        started_at = 0.0
        while True:
            if not self._user_stopped and not await self._cdp_ready():
                if started_at and time.monotonic() - started_at < CRASH_WINDOW:
                    crashes += 1
                    delay = min(300.0, WATCHDOG_INTERVAL * (2 ** min(crashes - 1, 4)))
                    log.warning("server browser went down %.0fs after starting (%d in a row); waiting %.0fs",
                                time.monotonic() - started_at, crashes, delay)
                    started_at = 0.0
                    await asyncio.sleep(delay)
                    continue
                if started_at:
                    crashes = 0
                try:
                    if failures > 0 or self._chrome is not None:
                        log.warning("server browser is offline; restarting")
                    await self.ensure_running()
                    failures = 0
                    started_at = time.monotonic()
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                    failures += 1
                    delay = min(300.0, WATCHDOG_INTERVAL * (2 ** min(failures - 1, 4)))
                    log.warning("server browser did not start (attempt %d): %s; retrying in %.0fs",
                                failures, exc, delay)
                    await asyncio.sleep(delay)
                    continue
            await asyncio.sleep(WATCHDOG_INTERVAL)

    async def ensure_running(self) -> None:
        if not self.enabled:
            raise RuntimeError("服务器浏览器已禁用")
        chrome_bin = self.chrome_bin
        if not chrome_bin:
            raise RuntimeError("未找到 Chrome / Chromium 可执行文件")
        self._user_stopped = False
        async with self._lock:
            if await self._cdp_ready():
                self._last_error = None
                return
            await self._stop_procs()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            for name in _SINGLETON_FILES:
                with contextlib.suppress(OSError):
                    (self.profile_dir / name).unlink()

            env = dict(os.environ)
            env.pop("RUNNER_TOKEN", None)
            use_headless = self.cfg.browser_headless == "true"
            xvfb_bin = shutil.which("Xvfb")
            if not use_headless and xvfb_bin:
                display = _pick_free_display()
                if display:
                    self._xvfb = await asyncio.create_subprocess_exec(
                        xvfb_bin, display, "-screen", "0", f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}x24",
                        "-nolisten", "tcp", "-ac",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    self._display = display
                    sock = Path(f"/tmp/.X11-unix/X{display[1:]}")
                    for _ in range(30):
                        if sock.exists() or self._xvfb.returncode is not None:
                            break
                        await asyncio.sleep(0.1)
                    if self._xvfb.returncode is None and sock.exists():
                        env["DISPLAY"] = display
                    else:
                        await self._kill_proc(self._xvfb)
                        self._xvfb = None
                        self._display = None
                        use_headless = True
                else:
                    use_headless = True
            elif not use_headless and not env.get("DISPLAY"):
                use_headless = True

            args = [
                chrome_bin,
                f"--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_dir}",
                f"--window-size={DEFAULT_WIDTH},{DEFAULT_HEIGHT}",
                "--window-position=0,0",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-gpu",
                "--lang=zh-CN",
            ]
            if use_headless:
                args.append("--headless=new")
            args.append("about:blank")

            log.info("starting server browser on 127.0.0.1:%d (%s)", self.port,
                     "headless" if use_headless else f"Xvfb {self._display}")
            self._stderr_tail.clear()
            self._chrome = await asyncio.create_subprocess_exec(
                *args, env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._chrome))
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if self._chrome.returncode is not None:
                    code = self._chrome.returncode
                    await asyncio.sleep(0.05)
                    err = " | ".join(list(self._stderr_tail)[-5:])
                    await self._stop_procs()                    # clears self._chrome
                    self._last_error = f"Chrome exited ({code}): {err[:240]}"
                    raise RuntimeError(self._last_error)
                if await self._cdp_ready():
                    self._last_error = None
                    return
                await asyncio.sleep(0.25)
            await self._stop_procs()
            self._last_error = "Chrome remote debugging port did not open in time"
            raise RuntimeError(self._last_error)

    async def stop(self, *, by_user: bool = True) -> None:
        if by_user:
            self._user_stopped = True
        async with self._lock:
            await self._stop_procs()

    async def _stop_procs(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
        if self._chrome is not None:
            await self._kill_proc(self._chrome)
            self._chrome = None
        if self._xvfb is not None:
            await self._kill_proc(self._xvfb)
            self._xvfb = None
            self._display = None

    @staticmethod
    async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)

    # ---- CDP HTTP tab management ----------------------------------------------------------

    async def _raw_tabs(self) -> list[dict[str, Any]]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                async with http.get(f"{self.base_url}/json/list") as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
        except Exception:  # noqa: BLE001
            return []
        return [t for t in data if isinstance(t, dict) and t.get("type") == "page" and t.get("id")]

    async def list_tabs(self) -> list[dict[str, Any]]:
        return [{"id": t["id"], "title": t.get("title") or t.get("url") or "新标签页", "url": t.get("url") or ""}
                for t in await self._raw_tabs()]

    async def new_tab(self, url: str = "about:blank") -> dict[str, Any]:
        await self.ensure_running()
        target_url = normalize_url(url)
        encoded = urllib.parse.quote(target_url, safe=":/?&=#%+~@")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            async with http.put(f"{self.base_url}/json/new?{encoded}") as resp:
                if resp.status != 200:
                    raise RuntimeError(f"cannot open tab (HTTP {resp.status})")
                t = await resp.json(content_type=None)
        return {"id": t["id"], "title": t.get("title") or target_url, "url": t.get("url") or target_url}

    async def activate_tab(self, target_id: str) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            async with http.get(f"{self.base_url}/json/activate/{urllib.parse.quote(target_id, safe='')}") as resp:
                if resp.status >= 400:
                    raise RuntimeError("标签页不存在")

    async def close_tab(self, target_id: str) -> None:
        tabs = await self._raw_tabs()
        if len(tabs) <= 1 and any(t["id"] == target_id for t in tabs):
            await self.new_tab("about:blank")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            await http.get(f"{self.base_url}/json/close/{urllib.parse.quote(target_id, safe='')}")

    async def status(self) -> dict[str, Any]:
        # A disabled browser is never reported as running, even if some other Chrome holds the port.
        running = self.available and await self._cdp_ready()
        tabs = await self.list_tabs() if running else []
        return {
            "enabled": self.enabled,
            "available": self.available,
            "running": running,
            "mcpReady": self.mcp_ready,
            "viewport": {"width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT},
            "tabs": tabs,
            "error": self._last_error,
        }

    # ---- Interactive CDP screencast + input relay -----------------------------------------

    async def bridge_ws(self, ws: web.WebSocketResponse, target_id: str | None = None) -> None:
        """Relay a tab's live screencast frames and user mouse/keyboard input over `ws`."""
        await self.ensure_running()
        raw = await self._raw_tabs()
        if not raw:
            await self.new_tab("about:blank")
            raw = await self._raw_tabs()
        target = next((t for t in raw if t["id"] == target_id), None) if target_id else None
        if target is None:
            target = raw[0]
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            await ws.send_str(json.dumps({"ev": "error", "message": "无法连接到该标签页"}))
            return

        with contextlib.suppress(Exception):
            await self.activate_tab(target["id"])

        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(ws_url, max_msg_size=32 * 1024 * 1024) as cdp:
                next_id = 0
                pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

                async def cdp_send(method: str, params: dict[str, Any] | None = None, wait: bool = False) -> dict[str, Any]:
                    nonlocal next_id
                    next_id += 1
                    cid = next_id
                    fut: asyncio.Future[dict[str, Any]] | None = None
                    if wait:
                        fut = asyncio.get_running_loop().create_future()
                        pending[cid] = fut
                    payload: dict[str, Any] = {"id": cid, "method": method}
                    if params:
                        payload["params"] = params
                    await cdp.send_str(json.dumps(payload))
                    if fut is not None:
                        try:
                            return await asyncio.wait_for(fut, timeout=10)
                        finally:
                            pending.pop(cid, None)
                    return {}

                async def from_cdp() -> None:
                    async for msg in cdp:
                        if msg.type != WSMsgType.TEXT:
                            if msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                                break
                            continue
                        try:
                            data = json.loads(msg.data)
                        except ValueError:
                            continue
                        if "id" in data:
                            fut = pending.get(data["id"])
                            if fut and not fut.done():
                                fut.set_result(data.get("result") or {})
                            continue
                        method = data.get("method")
                        params = data.get("params") or {}
                        if method == "Page.screencastFrame":
                            sid = params.get("sessionId")
                            if sid is not None:
                                await cdp_send("Page.screencastFrameAck", {"sessionId": sid})
                            meta = params.get("metadata") or {}
                            await ws.send_str(json.dumps({
                                "ev": "frame",
                                "data": params.get("data") or "",
                                "width": meta.get("deviceWidth") or DEFAULT_WIDTH,
                                "height": meta.get("deviceHeight") or DEFAULT_HEIGHT,
                            }))
                        elif method == "Page.frameNavigated":
                            frame = params.get("frame") or {}
                            if not frame.get("parentId"):
                                await ws.send_str(json.dumps({"ev": "nav", "url": frame.get("url") or ""}))
                        elif method == "Page.navigatedWithinDocument":
                            await ws.send_str(json.dumps({"ev": "nav", "url": params.get("url") or ""}))

                reader = asyncio.create_task(from_cdp())
                try:
                    await cdp_send("Page.enable")
                    await cdp_send("Page.bringToFront")
                    await cdp_send("Page.startScreencast", {
                        "format": "jpeg",
                        "quality": 72,
                        "maxWidth": DEFAULT_WIDTH,
                        "maxHeight": DEFAULT_HEIGHT,
                        "everyNthFrame": 1,
                    })
                    await ws.send_str(json.dumps({
                        "ev": "ready",
                        "targetId": target["id"],
                        "url": target.get("url") or "",
                        "title": target.get("title") or "",
                        "viewport": {"width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT},
                    }))
                    async for msg in ws:
                        if msg.type != WSMsgType.TEXT:
                            continue
                        try:
                            data = json.loads(msg.data)
                        except ValueError:
                            continue
                        op = data.get("op")
                        if op == "mouse":
                            mtype = data.get("type")
                            if mtype not in ("mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"):
                                continue
                            params: dict[str, Any] = {
                                "type": mtype,
                                "x": float(data.get("x") or 0),
                                "y": float(data.get("y") or 0),
                                "modifiers": int(data.get("modifiers") or 0),
                            }
                            if mtype in ("mousePressed", "mouseReleased"):
                                params["button"] = data.get("button") if data.get("button") in ("left", "right", "middle") else "left"
                                params["clickCount"] = max(1, min(3, int(data.get("clickCount") or 1)))
                            elif mtype == "mouseWheel":
                                params["deltaX"] = float(data.get("deltaX") or 0)
                                params["deltaY"] = float(data.get("deltaY") or 0)
                            await cdp_send("Input.dispatchMouseEvent", params)
                        elif op == "key":
                            ktype = data.get("type")
                            if ktype not in ("keyDown", "keyUp", "rawKeyDown", "char"):
                                continue
                            params = {
                                "type": ktype,
                                "modifiers": int(data.get("modifiers") or 0),
                            }
                            for k in ("key", "code", "text", "unmodifiedText"):
                                if isinstance(data.get(k), str):
                                    params[k] = data[k]
                            if isinstance(data.get("windowsVirtualKeyCode"), int):
                                params["windowsVirtualKeyCode"] = data["windowsVirtualKeyCode"]
                                params["nativeVirtualKeyCode"] = data["windowsVirtualKeyCode"]
                            await cdp_send("Input.dispatchKeyEvent", params)
                        elif op == "text":
                            text = data.get("text")
                            if isinstance(text, str) and text:
                                await cdp_send("Input.insertText", {"text": text})
                        elif op == "navigate":
                            url = normalize_url(str(data.get("url") or ""))
                            await cdp_send("Page.navigate", {"url": url})
                        elif op == "reload":
                            await cdp_send("Page.reload")
                        elif op == "history":
                            delta = int(data.get("delta") or 0)
                            hist = await cdp_send("Page.getNavigationHistory", wait=True)
                            entries = hist.get("entries") or []
                            idx = int(hist.get("currentIndex") or 0) + delta
                            if 0 <= idx < len(entries):
                                await cdp_send("Page.navigateToHistoryEntry", {"entryId": entries[idx]["id"]})
                        elif op == "ping":
                            await ws.send_str(json.dumps({"ev": "pong", "t": data.get("t")}))
                finally:
                    with contextlib.suppress(Exception):
                        await cdp_send("Page.stopScreencast")
                    reader.cancel()
                    for fut in pending.values():
                        if not fut.done():
                            fut.cancel()
