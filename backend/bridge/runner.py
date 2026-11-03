"""Async client for the host runner's Unix-socket API."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from django.conf import settings

_session: aiohttp.ClientSession | None = None
_session_loop: asyncio.AbstractEventLoop | None = None

BASE = "http://runner"


class RunnerUnavailable(Exception):
    pass


def session() -> aiohttp.ClientSession:
    """A shared client session bound to the current event loop."""
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session is None or _session.closed or _session_loop is not loop:
        connector = aiohttp.UnixConnector(path=settings.RUNNER_SOCKET)
        _session = aiohttp.ClientSession(
            connector=connector,
            headers={"Authorization": f"Bearer {settings.RUNNER_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
        )
        _session_loop = loop
    return _session


async def request(method: str, path: str, *, params: Any = None, json: Any = None, data: Any = None,
                  timeout: float = 120) -> aiohttp.ClientResponse:
    try:
        return await session().request(method, BASE + path, params=params, json=json, data=data,
                                       timeout=aiohttp.ClientTimeout(total=timeout))
    except (aiohttp.ClientConnectionError, FileNotFoundError, ConnectionRefusedError) as exc:
        raise RunnerUnavailable(str(exc)) from exc


async def ws_connect() -> aiohttp.ClientWebSocketResponse:
    try:
        return await session().ws_connect(BASE + "/v1/ws", heartbeat=30, max_msg_size=64 * 1024 * 1024)
    except (aiohttp.ClientConnectionError, FileNotFoundError, ConnectionRefusedError) as exc:
        raise RunnerUnavailable(str(exc)) from exc
