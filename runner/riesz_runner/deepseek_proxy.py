"""Localhost Anthropic-compatible proxy for DeepSeek API.

Newer Claude Code builds emit Anthropic-specific message roles and content block
variants (such as mid-conversation `role: "system"` entries, `tool_addition`
carrying `tool_definition`, `tool_removal`, and `tool_reference` inside
`tool_result`) both during live MCP changes and when resuming an existing
Anthropic session. DeepSeek's Anthropic-compatible endpoint rejects unknown
variants with HTTP 422.

This proxy listens strictly on `127.0.0.1:<ephemeral>`, sanitizes outbound
JSON request bodies in-flight without mutating any Claude transcript on disk,
and streams responses verbatim back to Claude Code. It never stores API keys:
Claude Code passes its own `x-api-key` / `Authorization` headers via its
configured `apiKeyHelper`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from aiohttp import web

from .routes import DEEPSEEK_ANTHROPIC_URL

log = logging.getLogger(__name__)

# Maximum request body size accepted from Claude Code (long transcripts + images).
MAX_REQUEST_BYTES = 128 * 1024 * 1024

_HOP_BY_HOP = frozenset({
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding",
    "accept-encoding",
})
_DROP_BLOCKS = frozenset({"tool_addition", "tool_removal", "tool_definition", "redacted_thinking"})
_KEEP_TOP_BLOCKS = frozenset({"text", "image", "thinking", "tool_use", "tool_result"})
_KEEP_TOOL_RESULT_BLOCKS = frozenset({"text", "image"})


def _tool_ref_text(block: dict[str, Any]) -> dict[str, Any]:
    name = block.get("tool_name") or block.get("name") or ""
    return {"type": "text", "text": f"[Loaded tool: {name}]" if name else "[Loaded tool]"}


def _clean_tool_result(block: dict[str, Any]) -> dict[str, Any]:
    content = block.get("content")
    if not isinstance(content, list):
        return block
    cleaned: list[Any] = []
    for sub in content:
        if not isinstance(sub, dict):
            cleaned.append(sub)
            continue
        st = sub.get("type")
        if st in _KEEP_TOOL_RESULT_BLOCKS:
            cleaned.append(sub)
        elif st == "tool_reference":
            cleaned.append(_tool_ref_text(sub))
        elif st in _DROP_BLOCKS:
            continue
        elif isinstance(sub.get("text"), str) and sub["text"]:
            cleaned.append({"type": "text", "text": sub["text"]})
        else:
            cleaned.append({"type": "text", "text": json.dumps(sub, ensure_ascii=False)[:1000]})
    return {**block, "content": cleaned or "OK"}


def _clean_block(block: Any) -> dict[str, Any] | Any | None:
    if not isinstance(block, dict):
        return block
    bt = block.get("type")
    if bt in _DROP_BLOCKS:
        return None
    if bt == "tool_reference":
        return _tool_ref_text(block)
    if bt == "tool_result":
        return _clean_tool_result(block)
    if bt in _KEEP_TOP_BLOCKS:
        return block
    if isinstance(block.get("text"), str) and block["text"]:
        return {"type": "text", "text": block["text"]}
    return None


def _as_blocks(content: Any) -> list[Any]:
    if isinstance(content, list):
        return list(content)
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return []


def _merge_user_blocks(first: list[Any], second: list[Any]) -> list[Any]:
    """Keep all tool_result blocks ahead of text/image blocks when merging consecutive user messages."""
    combined = [*first, *second]
    results = [b for b in combined if isinstance(b, dict) and b.get("type") == "tool_result"]
    others = [b for b in combined if not (isinstance(b, dict) and b.get("type") == "tool_result")]
    return [*results, *others]


def sanitize_anthropic_request(body: dict[str, Any]) -> dict[str, Any]:
    """Return a DeepSeek-compatible copy of an Anthropic Messages API request body."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body

    out_msgs: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            cleaned = [b for b in (_clean_block(blk) for blk in content) if b is not None]
        else:
            cleaned = content

        if role == "system":
            if isinstance(cleaned, str):
                texts = [cleaned] if cleaned.strip() else []
            elif isinstance(cleaned, list):
                texts = [
                    b["text"] for b in cleaned
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip()
                ]
            else:
                texts = []
            if not texts:
                continue
            normalized: dict[str, Any] = {
                "role": "user",
                "content": [{"type": "text", "text": "[System notice]\n" + "\n".join(texts)}],
            }
        else:
            if not cleaned:
                continue
            normalized = {**msg, "content": cleaned}

        if out_msgs and out_msgs[-1].get("role") == normalized.get("role"):
            prev_blocks = _as_blocks(out_msgs[-1].get("content"))
            curr_blocks = _as_blocks(normalized.get("content"))
            merged = (
                _merge_user_blocks(prev_blocks, curr_blocks)
                if normalized.get("role") == "user"
                else [*prev_blocks, *curr_blocks]
            )
            out_msgs[-1] = {**out_msgs[-1], "content": merged}
        else:
            out_msgs.append(normalized)

    return {**body, "messages": out_msgs}


class DeepSeekAnthropicProxy:
    """Loopback reverse proxy forwarding `/anthropic/*` to DeepSeek's Anthropic endpoint."""

    def __init__(self, upstream: str = DEEPSEEK_ANTHROPIC_URL) -> None:
        self.upstream = upstream.rstrip("/")
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self.port: int | None = None

    @property
    def base_url(self) -> str | None:
        return f"http://127.0.0.1:{self.port}/anthropic" if self.port is not None else None

    async def ensure_started(self) -> str:
        async with self._lock:
            if self.port is not None:
                return f"http://127.0.0.1:{self.port}/anthropic"
            app = web.Application(client_max_size=MAX_REQUEST_BYTES)
            app.router.add_route("*", "/anthropic/{tail:.*}", self._handle)
            app.router.add_route("*", "/anthropic", self._handle)
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            sockets = site._server.sockets if site._server else ()
            assert sockets, "failed to bind DeepSeek compatibility proxy socket"
            self.port = int(sockets[0].getsockname()[1])
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=30))
            log.info("DeepSeek Anthropic proxy listening on 127.0.0.1:%d -> %s", self.port, self.upstream)
            return f"http://127.0.0.1:{self.port}/anthropic"

    async def close(self) -> None:
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            self.port = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        assert self._session is not None
        tail = request.match_info.get("tail", "")
        target = f"{self.upstream}/{tail}" if tail else self.upstream
        if request.query_string:
            target = f"{target}?{request.query_string}"

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        raw = await request.read()
        data: bytes = raw
        if raw and "json" in (request.headers.get("content-type") or "").lower():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "messages" in parsed:
                    data = json.dumps(sanitize_anthropic_request(parsed), ensure_ascii=False).encode("utf-8")
            except ValueError:
                pass

        try:
            async with self._session.request(request.method, target, headers=headers, data=data or None) as upstream_resp:
                resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP}
                out = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
                await out.prepare(request)
                async for chunk in upstream_resp.content.iter_any():
                    if chunk:
                        await out.write(chunk)
                await out.write_eof()
                return out
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("DeepSeek proxy upstream error (%s): %s", tail or "/", exc)
            return web.json_response(
                {"type": "error", "error": {"type": "api_error", "message": f"DeepSeek proxy error: {exc}"}},
                status=502,
            )
