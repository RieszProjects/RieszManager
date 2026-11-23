"""Tests for the DeepSeek Anthropic-compatible request sanitizer and loopback proxy."""

from __future__ import annotations

import json
import unittest

import aiohttp
from aiohttp import web

from riesz_runner.deepseek_proxy import DeepSeekAnthropicProxy, sanitize_anthropic_request


class SanitizeTest(unittest.TestCase):
    def test_strips_tool_addition_removal_and_converts_system_and_tool_ref(self) -> None:
        req = {
            "model": "deepseek-flash",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "ToolSearch", "input": {}}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {"type": "tool_reference", "tool_name": "mcp__browser__click"},
                                {"type": "tool_definition", "definition": {"name": "x"}},
                            ],
                        }
                    ],
                },
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "MCP tools updated"},
                        {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {"name": "a"}}},
                        {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "b"}},
                    ],
                },
                {
                    "role": "system",
                    "content": [
                        {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "c"}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "opaque"},
                        {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                        {"type": "text", "text": "done"},
                    ],
                },
            ],
        }
        out = sanitize_anthropic_request(req)
        msgs = out["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        # Merged user message keeps tool_result first and converts tool_reference -> text, dropping tool_definition.
        u2 = msgs[2]["content"]
        self.assertEqual(u2[0]["type"], "tool_result")
        self.assertEqual(u2[0]["content"], [{"type": "text", "text": "[Loaded tool: mcp__browser__click]"}])
        self.assertEqual(u2[1]["type"], "text")
        self.assertIn("MCP tools updated", u2[1]["text"])
        # Redacted thinking dropped, regular thinking preserved
        self.assertEqual([b["type"] for b in msgs[3]["content"]], ["thinking", "text"])

    def test_system_before_tool_result_preserves_tool_result_first(self) -> None:
        req = {
            "model": "deepseek-flash",
            "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
                {"role": "system", "content": [{"type": "text", "text": "reminder"}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            ],
        }
        out = sanitize_anthropic_request(req)
        self.assertEqual([m["role"] for m in out["messages"]], ["assistant", "user"])
        self.assertEqual([b["type"] for b in out["messages"][1]["content"]], ["tool_result", "text"])


class ProxyStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_sanitizes_and_streams_sse(self) -> None:
        seen: list[tuple[str, str | None, dict]] = []

        async def upstream_handler(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            seen.append((request.path, request.headers.get("x-api-key"), body))
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b"event: ping\ndata: {\"ok\": true}\n\n")
            await resp.write_eof()
            return resp

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", upstream_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        up_port = site._server.sockets[0].getsockname()[1]

        proxy = DeepSeekAnthropicProxy(upstream=f"http://127.0.0.1:{up_port}/anthropic")
        base_url = await proxy.ensure_started()
        try:
            async with aiohttp.ClientSession() as sess:
                payload = {
                    "model": "deepseek-flash",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                        {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_definition"}}]},
                    ],
                }
                async with sess.post(
                    f"{base_url}/v1/messages",
                    headers={"x-api-key": "secret-test", "content-type": "application/json"},
                    data=json.dumps(payload),
                ) as resp:
                    self.assertEqual(resp.status, 200)
                    text = await resp.text()
                    self.assertIn("event: ping", text)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], "/anthropic/v1/messages")
            self.assertEqual(seen[0][1], "secret-test")
            self.assertEqual(len(seen[0][2]["messages"]), 1)
        finally:
            await proxy.close()
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
