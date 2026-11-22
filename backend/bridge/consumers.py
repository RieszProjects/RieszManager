"""WebSocket endpoint relaying live events between the browser and the runner."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils import timezone

from accounts.services import totp_device
from push import presence

from . import runner

log = logging.getLogger(__name__)

ALLOWED_OPS = {"sub", "unsub", "sub_global", "ping"}
SESSION_RECHECK_SECONDS = 60
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@database_sync_to_async
def _session_valid(session_key: str | None) -> bool:
    if not session_key:
        return False
    return Session.objects.filter(session_key=session_key, expire_date__gt=timezone.now()).exists()


@database_sync_to_async
def _totp_ok(username: str) -> bool:
    return not settings.RIESZ_REQUIRE_TOTP or totp_device(username) is not None


class EventsConsumer(AsyncWebsocketConsumer):
    upstream: aiohttp.ClientWebSocketResponse | None = None
    relay_task: asyncio.Task[Any] | None = None
    watch_task: asyncio.Task[Any] | None = None

    async def connect(self) -> None:
        user = self.scope.get("user")
        if user is None or not user.is_authenticated or not await _totp_ok(user.username):
            await self.close(code=4401)
            return
        try:
            self.upstream = await runner.ws_connect()
        except runner.RunnerUnavailable:
            await self.accept()
            await self.send(text_data=json.dumps({"ev": "runner_unavailable"}))
            await self.close(code=4503)
            return
        await self.accept()
        self.relay_task = asyncio.create_task(self._relay())
        self.watch_task = asyncio.create_task(self._watch_session())

    async def _relay(self) -> None:
        assert self.upstream is not None
        try:
            async for msg in self.upstream:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self.send(text_data=msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception:  # noqa: BLE001
            log.exception("runner relay failed")
        finally:
            await self.close(code=4503)

    async def _watch_session(self) -> None:
        """Close the socket if the login session is revoked or expires."""
        session = self.scope.get("session")
        key = getattr(session, "session_key", None)
        while True:
            await asyncio.sleep(SESSION_RECHECK_SECONDS)
            if not await _session_valid(key):
                await self.close(code=4401)
                return

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        if not text_data or self.upstream is None:
            return
        try:
            data = json.loads(text_data)
        except ValueError:
            return
        op = data.get("op") if isinstance(data, dict) else None
        if op == "presence":
            # Handled here, not forwarded: which session this browser shows (see push/presence.py).
            sid = str(data.get("sessionId") or "")
            presence.report(self.channel_name, sid if _UUID_RE.match(sid) else None, bool(data.get("active")))
            return
        if op not in ALLOWED_OPS:
            return
        message: dict[str, Any] = {"op": op}
        if op in ("sub", "unsub"):
            sid = str(data.get("sessionId") or "")
            if not _UUID_RE.match(sid):
                return
            message["sessionId"] = sid
        if op == "ping":
            message["t"] = data.get("t")
        await self.upstream.send_str(json.dumps(message))

    async def disconnect(self, code: int) -> None:
        presence.forget(self.channel_name)
        for task in (self.relay_task, self.watch_task):
            if task is not None:
                task.cancel()
        if self.upstream is not None and not self.upstream.closed:
            await self.upstream.close()


class BrowserConsumer(AsyncWebsocketConsumer):
    """Authenticated WebSocket relay for the interactive server-browser CDP viewer."""

    upstream: aiohttp.ClientWebSocketResponse | None = None
    relay_task: asyncio.Task[Any] | None = None
    watch_task: asyncio.Task[Any] | None = None

    async def connect(self) -> None:
        from urllib.parse import parse_qs

        user = self.scope.get("user")
        if user is None or not user.is_authenticated or not await _totp_ok(user.username):
            await self.close(code=4401)
            return
        qs = parse_qs((self.scope.get("query_string") or b"").decode("utf-8", "ignore"))
        target = (qs.get("target") or [""])[0]
        params = {"target": target} if target and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target) else None
        try:
            self.upstream = await runner.ws_connect("/v1/browser/ws", params=params)
        except runner.RunnerUnavailable:
            await self.accept()
            await self.send(text_data=json.dumps({"ev": "error", "message": "宿主机运行器未连接"}))
            await self.close(code=4503)
            return
        await self.accept()
        self.relay_task = asyncio.create_task(self._relay())
        self.watch_task = asyncio.create_task(self._watch_session())

    async def _relay(self) -> None:
        assert self.upstream is not None
        try:
            async for msg in self.upstream:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self.send(text_data=msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception:  # noqa: BLE001
            log.exception("browser ws relay failed")
        finally:
            await self.close(code=4503)

    async def _watch_session(self) -> None:
        session = self.scope.get("session")
        key = getattr(session, "session_key", None)
        while True:
            await asyncio.sleep(SESSION_RECHECK_SECONDS)
            if not await _session_valid(key):
                await self.close(code=4401)
                return

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        if not text_data or self.upstream is None or self.upstream.closed:
            return
        await self.upstream.send_str(text_data)

    async def disconnect(self, code: int) -> None:
        for task in (self.relay_task, self.watch_task):
            if task is not None:
                task.cancel()
        if self.upstream is not None and not self.upstream.closed:
            await self.upstream.close()


class NodeConsumer(AsyncWebsocketConsumer):
    """Reverse WebSocket tunnel for personal computer nodes (`riesz-node`).

    Authentication happens inside the runner via Ed25519 challenge-response against the
    device's registered public key; no browser session cookie is required or trusted here.
    """

    upstream: aiohttp.ClientWebSocketResponse | None = None
    relay_task: asyncio.Task[Any] | None = None

    async def connect(self) -> None:
        try:
            self.upstream = await runner.ws_connect("/v1/nodes/ws")
        except runner.RunnerUnavailable:
            await self.close(code=4503)
            return
        await self.accept()
        self.relay_task = asyncio.create_task(self._relay())

    async def _relay(self) -> None:
        assert self.upstream is not None
        close_code = 1000
        try:
            async for msg in self.upstream:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self.send(text_data=msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    if self.upstream.close_code:
                        close_code = self.upstream.close_code
                    break
        except Exception:  # noqa: BLE001
            log.exception("node ws relay failed")
            close_code = 4503
        finally:
            await self.close(code=close_code)

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        if not text_data or self.upstream is None or self.upstream.closed:
            return
        await self.upstream.send_str(text_data)

    async def disconnect(self, code: int) -> None:
        if self.relay_task is not None:
            self.relay_task.cancel()
        if self.upstream is not None and not self.upstream.closed:
            await self.upstream.close()
