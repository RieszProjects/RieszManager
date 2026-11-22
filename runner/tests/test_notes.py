"""Tests for reader sticky notes (notes.py, MCP tools note_write/note_list/note_delete, and HTTP API)."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web

from riesz_runner import tools as tools_mod
from riesz_runner.config import Config
from riesz_runner.notes import NoteError, Notes
from riesz_runner.server import HUB, build_app

RELAY = Path(tools_mod.__file__).with_name("tool_server.py")


class _Relay:
    def __init__(self, socket: Path) -> None:
        self.socket = socket
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0

    async def __aenter__(self) -> "_Relay":
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(RELAY), "--socket", str(self.socket),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        return self

    async def __aexit__(self, *exc) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()

    async def rpc(self, method: str, params: dict | None = None) -> dict:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(req) + "\n").encode())
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=10)
        return json.loads(line.decode())

    async def call(self, name: str, **args) -> tuple[str, bool]:
        res = (await self.rpc("tools/call", {"name": name, "arguments": args}))["result"]
        return res["content"][0]["text"], bool(res.get("isError"))


class NotesUnitTest(unittest.IsolatedAsyncioTestCase):
    async def test_persistence_crud_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            events: list[dict] = []

            async def broadcast(ev: dict) -> None:
                events.append(ev)

            hub = SimpleNamespace(cfg=SimpleNamespace(state_dir=Path(td)), broadcast_global=broadcast)
            notes = Notes(hub)  # type: ignore[arg-type]

            n1, created1 = await notes.upsert("s1", "选 C1 + T3 + F1", title="待确认方案", color="amber")
            self.assertTrue(created1)
            self.assertEqual(n1["title"], "待确认方案")
            self.assertEqual(n1["color"], "amber")
            self.assertEqual(events[-1]["ev"], "notes")
            self.assertEqual(events[-1]["action"], "create")

            n1_u, created_u = await notes.upsert("s1", "已改为 C2 + T1 + F2", note_id=n1["id"], color="sage")
            self.assertFalse(created_u)
            self.assertEqual(n1_u["title"], "待确认方案")
            self.assertEqual(n1_u["content"], "已改为 C2 + T1 + F2")
            self.assertEqual(n1_u["color"], "sage")

            n2, _ = await notes.upsert("s2", "另一个会话的便签", title="端口")
            self.assertEqual([n["id"] for n in notes.list({"s1"})], [n1["id"]])
            self.assertEqual(len(notes.list()), 2)

            # Reload from disk
            reloaded = Notes(hub)  # type: ignore[arg-type]
            self.assertEqual(len(reloaded.list()), 2)
            self.assertEqual(reloaded.get(n1["id"])["content"], "已改为 C2 + T1 + F2")  # type: ignore[index]

            # Delete & error on missing
            await notes.delete(n2["id"])
            self.assertIsNone(notes.get(n2["id"]))
            with self.assertRaises(NoteError):
                await notes.delete("deadbeef")
            with self.assertRaises(NoteError):
                await notes.upsert("s1", "   ")
            with self.assertRaises(NoteError):
                await notes.upsert("s1", "ok", color="neon")


class NotesMcpAndHttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        self.sid = "33333333-3333-3333-3333-333333333333"
        self.cfg = Config(
            workspace_root=(base / "ws").resolve(),
            claude_home=base / "claude",
            claude_bin="claude",
            state_dir=base / "state",
            socket_path=base / "runner.sock",
            token="n" * 32,
            browser_enabled=False,
        )
        self.cfg.workspace_root.mkdir(parents=True)
        self.app = build_app(self.cfg)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.UnixSite(self.runner, str(self.cfg.socket_path))
        await self.site.start()

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self._td.cleanup()

    async def test_mcp_note_tools_roundtrip(self):
        with mock.patch.object(tools_mod, "caller_session", return_value=self.sid):
            async with _Relay(self.cfg.socket_path) as relay:
                listed = await relay.rpc("tools/list")
                names = {t["name"] for t in listed["result"]["tools"]}
                self.assertTrue({"note_write", "note_list", "note_delete"} <= names)

                text, err = await relay.call("note_write", title="测试账号", content="账号 demo / 端口 8080", color="slate")
                self.assertFalse(err, text)
                self.assertIn("已新建便签", text)
                note_id = self.app[HUB].notes.list()[0]["id"]

                text2, err2 = await relay.call("note_list")
                self.assertFalse(err2)
                self.assertIn(note_id, text2)
                self.assertIn("测试账号", text2)

                text3, err3 = await relay.call("note_write", id=note_id, content="端口已改 9090")
                self.assertFalse(err3, text3)
                self.assertIn("已更新便签", text3)
                self.assertEqual(self.app[HUB].notes.get(note_id)["content"], "端口已改 9090")

                text4, err4 = await relay.call("note_delete", id=note_id)
                self.assertFalse(err4, text4)
                self.assertEqual(self.app[HUB].notes.list(), [])
