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
                  headers: dict[str, str] | None = None, timeout: float = 120,
                  stream: bool = False) -> aiohttp.ClientResponse:
    """`stream`: the body is relayed as it arrives (files, media, node transfers), so `timeout` limits how
    long the runner may go silent, not the whole transfer (a total limit would cut slow downloads short)."""
    limit = (aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=timeout) if stream
             else aiohttp.ClientTimeout(total=timeout))
    try:
        return await session().request(method, BASE + path, params=params, json=json, data=data, headers=headers,
                                       timeout=limit)
    except (aiohttp.ClientError, asyncio.TimeoutError, FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise RunnerUnavailable(str(exc)) from exc


async def ws_connect(path: str = "/v1/ws", *, params: Any = None) -> aiohttp.ClientWebSocketResponse:
    try:
        return await session().ws_connect(BASE + path, params=params, heartbeat=30, max_msg_size=64 * 1024 * 1024)
    except (aiohttp.ClientError, asyncio.TimeoutError, FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise RunnerUnavailable(str(exc)) from exc
