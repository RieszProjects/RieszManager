"""Context window size and auto-compact point of a model, as Claude Code sees them.

Claude Code compacts a conversation automatically when its context reaches the
window size minus a reserved "autocompact buffer". Both numbers depend on the
model and on the user's settings, so they are read from Claude Code itself:
`get_context_usage` answered by a short-lived `claude -p` process that is never
sent a prompt (no model request, nothing persisted). Running sessions answer
the same request directly; see server.context_limits.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from .config import Config
from .process import ClaudeProcess

CACHE_SECONDS = 6 * 3600
PROBE_TIMEOUT = 45


def limits_from_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Pick the window size and compaction buffer out of a get_context_usage answer."""
    max_tokens = usage.get("maxTokens") or usage.get("rawMaxTokens")
    buffer = 0
    for cat in usage.get("categories") or []:
        if isinstance(cat, dict) and (cat.get("kind") == "buffer" or cat.get("id") == "autocompact_buffer"):
            buffer += int(cat.get("tokens") or 0)
    return {"maxTokens": max_tokens, "autocompactBuffer": buffer}


class ContextLimits:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, model: str | None) -> dict[str, Any]:
        key = model or ""
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._cache.get(key)
            if cached and time.time() - cached[0] < CACHE_SECONDS:
                return cached[1]
            info = limits_from_usage(await self._probe(model))
            self._cache[key] = (time.time(), info)
            return info

    async def _probe(self, model: str | None) -> dict[str, Any]:
        cwd = self.cfg.state_dir / "probe"
        cwd.mkdir(parents=True, exist_ok=True)
        args = [self.cfg.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
                "--input-format", "stream-json", "--no-session-persistence", "--strict-mcp-config"]
        if model:
            args += ["--model", model]
        if self.cfg.login_shell:
            shell = os.environ.get("SHELL") or "/bin/bash"
            args = [shell, "-lc", 'exec "$0" "$@"', *args]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=ClaudeProcess._child_env(), limit=16 * 1024 * 1024,
        )
        assert proc.stdin and proc.stdout
        for rid, subtype in (("init", "initialize"), ("ctx", "get_context_usage")):
            proc.stdin.write((json.dumps({"type": "control_request", "request_id": rid,
                                          "request": {"subtype": subtype}}) + "\n").encode())
        await proc.stdin.drain()
        result: dict[str, Any] = {}
        try:
            async def read() -> None:
                nonlocal result
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        return
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    resp = msg.get("response") if msg.get("type") == "control_response" else None
                    if isinstance(resp, dict) and resp.get("request_id") == "ctx":
                        result = resp.get("response") or {}
                        return
            await asyncio.wait_for(read(), timeout=PROBE_TIMEOUT)
        finally:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
        if not result.get("maxTokens"):
            raise RuntimeError("claude did not report its context window")
        return result
