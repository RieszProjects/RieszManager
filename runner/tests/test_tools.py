"""Riesz Manager's own tools (tools.py) through the MCP relay a harness starts (tool_server.py).

The runner listens on a real Unix socket; the relay runs as a child of this test process, which a
fake harness session claims as its harness process, so the runner takes the relay's calls as that
session's. Also: how each harness is told to start the relay (Driver.mount_tools, --mcp-config).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from riesz_runner import harnesses as harnesses_mod
from riesz_runner import tools as tools_mod
from riesz_runner.config import Config
from riesz_runner.drivers.acp import AcpDriver
from riesz_runner.drivers.antigravity import AntigravityDriver
from riesz_runner.drivers.codex import CodexDriver
from riesz_runner.drivers.dsh import DshDriver
from riesz_runner.drivers.gemini import GeminiDriver
from riesz_runner.process import ClaudeProcess
from riesz_runner.server import HUB, build_app
from tests.test_harnesses import TOKEN, FakeDriver

RELAY = Path(tools_mod.__file__).with_name("tool_server.py")
AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class Relay:
    """The MCP server as a harness runs it: JSON-RPC lines on its standard input and output."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.n = 0

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params is not None else {})}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), 30)
        reply = json.loads(line)
        assert reply["id"] == self.n, reply
        return reply

    async def call(self, tool: str, /, **arguments: Any) -> tuple[str, bool]:
        res = (await self.request("tools/call", {"name": tool, "arguments": arguments}))["result"]
        return res["content"][0]["text"], res["isError"]


class ToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rz-tools-"))
        self.workspace = self.tmp / "work"
        self.workspace.mkdir()
        harnesses_mod.HARNESSES["fake"] = harnesses_mod.HarnessSpec(
            id="fake", name="Fake", label="Fake", bin="sh", driver=FakeDriver, arg="fake_bin", native_provider="deepseek",
            resume_command=None, handoff="first-message", fork="copy", commands={"compact": "method:compact"},
            modes={"readonly": "ask", "ask": "ask", "edits": "ask", "auto": "go", "full": "go"}, tools="mcp")
        self.addCleanup(harnesses_mod.HARNESSES.pop, "fake", None)
        self.socket = self.tmp / "r.sock"
        self.cfg = Config(workspace_root=self.tmp, claude_home=self.tmp / "claude", claude_bin="claude",
                          state_dir=self.tmp / "state", socket_path=self.socket, token=TOKEN)
        self.app = build_app(self.cfg)
        self.site_runner = web.AppRunner(self.app)
        await self.site_runner.setup()
        await web.UnixSite(self.site_runner, str(self.socket)).start()
        self.addAsyncCleanup(self.site_runner.cleanup)
        self.http = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(self.socket)))
        self.addAsyncCleanup(self.http.close)

    async def api(self, method: str, path: str, body: Any = None) -> Any:
        async with self.http.request(method, "http://runner/v1/" + path, json=body,
                                     headers={"Authorization": f"Bearer {TOKEN}"}) as res:
            data = await res.json()
            self.assertLess(res.status, 400, data)
            return data

    async def session(self, text: str) -> str:
        sid = (await self.api("POST", "sessions", {"cwd": str(self.workspace), "harness": "fake", "text": text}))["sessionId"]
        await self.idle(sid)
        return sid

    async def idle(self, sid: str) -> None:
        deadline = time.time() + 5
        while (await self.api("GET", f"sessions/{sid}"))["state"].get("busy"):
            self.assertLess(time.time(), deadline, "the turn ends")
            await asyncio.sleep(0.02)

    async def relay(self) -> Relay:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-I", str(RELAY), "--socket", str(self.socket),
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

        async def close() -> None:
            proc.stdin.close()
            await proc.wait()
        self.addAsyncCleanup(close)
        relay = Relay(proc)
        init = await relay.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "riesz")
        self.assertIn("Riesz Manager", init["result"]["instructions"])
        return relay

    async def test_tools_act_for_the_session_whose_harness_started_the_relay(self):
        me = await self.session("one")
        other = await self.session("two")
        self.app[HUB].harnesses.drivers[me].fake_pid = os.getpid()      # the relay below is our child
        relay = await self.relay()

        names = {t["name"] for t in (await relay.request("tools/list"))["result"]["tools"]}
        self.assertEqual(names, set(tools_mod.BY_NAME))
        # A newer protocol than we know: answered with ours (the client then decides).
        newer = await relay.request("initialize", {"protocolVersion": "2099-01-01"})
        self.assertEqual(newer["result"]["protocolVersion"], "2025-11-25")

        text, error = await relay.call("schedule_add", message="看看进度（{now}）", every="30m", name="巡检")
        self.assertFalse(error, text)
        self.assertIn("巡检", text)
        [sched] = (await self.api("GET", f"schedules?session={me}"))["schedules"]
        self.assertEqual(sched["every"], 30)
        text, error = await relay.call("schedule_list")
        self.assertIn(sched["id"], text)
        text, error = await relay.call("schedule_change", id=sched["id"], action="pause")
        self.assertIn("已暂停", text)
        text, error = await relay.call("schedule_add", message="x", at=["09:00"], every="1h")
        self.assertTrue(error)
        text, error = await relay.call("schedule_change", id=sched["id"], action="remove")
        self.assertFalse(error, text)
        self.assertEqual((await self.api("GET", f"schedules?session={me}"))["schedules"], [])

        text, error = await relay.call("session_send", session_id=other[:8], message="你好")
        self.assertFalse(error, text)
        await self.idle(other)
        text, error = await relay.call("session_reply", session_id=other)
        self.assertIn("echo: 你好", text)
        text, error = await relay.call("session_send", session_id=me, message="自己")
        self.assertTrue(error)
        text, error = await relay.call("session_list")
        self.assertIn(f"{me[:8]}  fake", text)
        self.assertIn("你自己", text)

        text, error = await relay.call("session_new", message="新的", harness="fake")
        self.assertFalse(error, text)
        started = [s for s in (await self.api("GET", "sessions"))["sessions"] if s.get("startedBy") == me]
        self.assertEqual(len(started), 1)

    async def test_send_file(self):
        me = await self.session("one")
        driver = self.app[HUB].harnesses.drivers[me]
        driver.fake_pid = os.getpid()
        (self.workspace / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.workspace / "notes.md").write_text("# 结论\n", encoding="utf-8")
        relay = await self.relay()
        text, error = await relay.call("send_file", paths=["plot.png", str(self.workspace / "notes.md")], caption="结果")
        self.assertFalse(error, text)
        self.assertIn("plot.png", text)
        files = (await self.api("GET", f"sessions/{me}/shared"))["files"]
        self.assertEqual(sorted(f["name"] for f in files), ["notes.md", "plot.png"])
        md = next(f for f in files if f["name"] == "notes.md")
        self.assertEqual((md["project"], md["mime"], md["caption"]), (f"@shared/{me}", "text/markdown", "结果"))
        # The viewer opens it like any file, from the copy (the original may change or go).
        (self.workspace / "notes.md").unlink()
        shown = (await self.api("GET", f"files/content?project=@shared/{me}&path={md['ref']}"))["file"]
        self.assertEqual(shown["content"], "# 结论\n")
        text, error = await relay.call("send_file", path="missing.txt")
        self.assertTrue(error)
        self.assertIn("不存在", text)

    async def test_open_a_linked_file(self):
        me = await self.session("one")
        (self.workspace / "a.txt").write_text("x")
        res = await self.api("POST", f"sessions/{me}/open-file", {"path": f"file://{self.workspace}/a.txt"})
        self.assertEqual(res, {"project": "work", "path": "a.txt"})
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("no")
        async with self.http.post(f"http://runner/v1/sessions/{me}/open-file", json={"path": str(outside)},
                                  headers={"Authorization": f"Bearer {TOKEN}"}) as resp:
            self.assertEqual(resp.status, 403)
        # A file the harness wrote for the user in its own folder: sent, then opened from the copy.
        art = Path(tempfile.mkdtemp())
        FakeDriver.artifact_dirs = (str(art / "{id}"),)
        self.addCleanup(setattr, FakeDriver, "artifact_dirs", ())
        (art / me).mkdir()
        (art / me / "plan.md").write_text("计划")
        res = await self.api("POST", f"sessions/{me}/open-file", {"path": f"file://{art}/{me}/plan.md"})
        self.assertEqual(res["project"], f"@shared/{me}")
        self.assertEqual((await self.api("GET", f"files/content?project=@shared/{me}&path={res['path']}"))["file"]["content"], "计划")

    async def test_a_process_outside_hosted_sessions_is_refused(self):
        await self.session("one")                                      # claims no process
        relay = await self.relay()                                     # the list is for anyone (a harness asks while it starts)
        self.assertTrue((await relay.request("tools/list"))["result"]["tools"])
        text, error = await relay.call("schedule_list")
        self.assertTrue(error)
        self.assertIn("托管", text)

    async def test_runner_down(self):
        proc = await asyncio.create_subprocess_exec(sys.executable, "-I", str(RELAY), "--socket", str(self.tmp / "none.sock"),
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        relay = Relay(proc)
        self.assertIn("result", await relay.request("initialize", {"protocolVersion": "2025-06-18"}))
        text, error = await relay.call("schedule_list")
        self.assertTrue(error)
        self.assertIn("runner", text)
        proc.stdin.close()
        await proc.wait()


class MountTest(unittest.IsolatedAsyncioTestCase):
    """Each harness is told, its own way, to start the relay."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rz-mount-"))
        self.cfg = Config(workspace_root=self.tmp, claude_home=self.tmp / "claude", claude_bin="claude",
                          state_dir=self.tmp / "state", socket_path=self.tmp / "r.sock", token=TOKEN)
        self.server = tools_mod.server(self.cfg)

    def test_ancestry(self):
        self.assertEqual(tools_mod.ancestry(os.getpid())[:2], [os.getpid(), os.getppid()])

    def test_claude_code(self):
        proc = ClaudeProcess(self.cfg, session_id="0" * 8 + "-0000-4000-8000-" + "0" * 12, cwd=str(self.tmp), sink=None,
                             tools=self.server)
        args = proc._command()
        config = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["riesz"], {"type": "stdio", "command": sys.executable,
                                                         "args": list(self.server.args)})

    def test_codex(self):
        args = CodexDriver(cwd="/", sink=None).mount_tools(self.server)
        self.assertEqual(args[0::2], ["-c", "-c"])
        self.assertEqual(args[1], f"mcp_servers.riesz.command={json.dumps(sys.executable)}")
        self.assertEqual(json.loads(args[3].split("=", 1)[1]), list(self.server.args))

    def test_dsh(self):
        path = Path(DshDriver(cwd="/", sink=None)._tools_patch(self.server))
        text = path.read_text()
        self.assertTrue(text.startswith("- insert: "))
        [entry] = json.loads(text[len("- insert: "):])
        self.assertEqual(entry, {"id": "mcp-riesz", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "riesz", "transport": "stdio", "command": sys.executable, "args": list(self.server.args)}})
        self.assertEqual(path.parent, self.cfg.state_dir / "tools")

    async def test_acp_session_parameters(self):
        for cls, kw in ((GeminiDriver, "gemini"), (AntigravityDriver, "agy")):
            with self.subTest(driver=cls.__name__):
                record = self.tmp / f"{kw}.jsonl"
                env = {**os.environ, "FAKE_ACP_RECORD": str(record)}

                async def sink(_driver: Any, _event: dict[str, Any]) -> None:
                    pass
                driver = cls(cwd=str(self.tmp), sink=sink, env=env, tools=self.server, **{kw: AGENT})
                self.assertIsInstance(driver, AcpDriver)
                await driver.start()
                await driver.stop()
                [new] = [json.loads(line) for line in record.read_text().splitlines()]
                self.assertEqual(new["method"], "session/new")
                self.assertEqual(new["params"]["mcpServers"], [{"name": "riesz", "command": sys.executable,
                                                                "args": list(self.server.args), "env": []}])


if __name__ == "__main__":
    unittest.main()
