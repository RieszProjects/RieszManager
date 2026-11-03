"""Fetch slash commands and models for a directory without starting a conversation.

Spawns a short-lived `claude` process that is never sent a prompt (so it makes
no API calls and persists no session), performs the `initialize` handshake and
exits. Results are cached per working directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from .config import Config
from .process import ClaudeProcess

CACHE_SECONDS = 600


class CapabilityProbe:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def remember(self, cwd: str, info: dict[str, Any]) -> None:
        self._cache[cwd] = (time.time(), info)

    async def get(self, cwd: str) -> dict[str, Any]:
        cached = self._cache.get(cwd)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        lock = self._locks.setdefault(cwd, asyncio.Lock())
        async with lock:
            cached = self._cache.get(cwd)
            if cached and time.time() - cached[0] < CACHE_SECONDS:
                return cached[1]
            info = await self._probe(cwd)
            self._cache[cwd] = (time.time(), info)
            return info

    async def _probe(self, cwd: str) -> dict[str, Any]:
        args = [self.cfg.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
                "--input-format", "stream-json", "--no-session-persistence"]
        if self.cfg.login_shell:
            shell = os.environ.get("SHELL") or "/bin/bash"
            args = [shell, "-lc", 'exec "$0" "$@"', *args]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=ClaudeProcess._child_env(), limit=64 * 1024 * 1024,
        )
        assert proc.stdin and proc.stdout
        request = {"type": "control_request", "request_id": "probe", "request": {"subtype": "initialize"}}
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        info: dict[str, Any] = {}
        try:
            async def read() -> None:
                nonlocal info
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        return
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("type") == "control_response":
                        info = (msg.get("response") or {}).get("response") or {}
                        return
            await asyncio.wait_for(read(), timeout=60)
        finally:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
        return {
            "commands": info.get("commands") or [],
            "models": info.get("models") or [],
            "agents": info.get("agents") or [],
            "permissionMode": info.get("current_permission_mode"),
        }
