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

    async def test_subagents(self):
        me = await self.session("one")
        self.app[HUB].harnesses.drivers[me].fake_pid = os.getpid()
        relay = await self.relay()
        text, error = await relay.call("subagent_start", task="查一下", harness="fake")
        self.assertFalse(error, text)
        self.assertIn("echo: [子任务，来自 Fake 会话", text)           # its reply, the task marked as from an agent
        child = text.split("子代理 ")[1][:8]
        [entry] = (await self.api("GET", f"sessions/{me}/subagents"))["subagents"]
        self.assertEqual((entry["sessionId"][:8], entry["via"], entry["harness"], entry["description"]), (child, "riesz", "fake", "查一下"))
        listed = [s for s in (await self.api("GET", "sessions"))["sessions"] if s["sessionId"].startswith(child)]
        self.assertEqual(listed[0]["subagentOf"], me)                  # the web app lists it under `me` only
        text, error = await relay.call("subagent_send", agent_id=child, message="再说一次")
        self.assertFalse(error, text)
        self.assertIn("再说一次", text)
        other = await self.session("two")                              # not started by `me`
        text, error = await relay.call("subagent_send", agent_id=other, message="x")
        self.assertTrue(error)
        text, error = await relay.call("subagent_start", task="x", harness="nope")
        self.assertTrue(error)

    async def test_subagent_autonomous_followup_and_background_task_callbacks(self):
        from riesz_runner.drivers.base import assistant_item, tool_block, tool_result

        me = await self.session("parent")
        hub = self.app[HUB]
        hub.harnesses.drivers[me].fake_pid = os.getpid()
        parent_driver = hub.harnesses.drivers[me]
        parent_msgs = lambda: [it["text"] for it in parent_driver.items.items if it.get("kind") == "user"]
        relay = await self.relay()

        # 1) Sync wait=True returns Turn 1 directly (no duplicate callback), then an autonomous
        #    tool-only turn is deduped, then an autonomous Turn 3 with new text notifies the parent.
        text, error = await relay.call("subagent_start", task="先试点生图", harness="fake")
        self.assertFalse(error, text)
        child_prefix = text.split("子代理 ")[1][:8]
        child = next(s for s in hub.harnesses.drivers if s.startswith(child_prefix))
        child_driver = hub.harnesses.drivers[child]
        await asyncio.sleep(0.05)
        self.assertEqual(len(parent_msgs()), 1)                       # only the initial "parent" prompt; no dup for Turn 1

        # Silent tool-only autonomous turn (no text blocks) -> must NOT re-send Turn 1.
        child_driver.items.put(assistant_item("auto:toolonly", [tool_block("t1", "Bash", {"command": "echo"}, tool_result("ok"))]))
        await child_driver.emit({"ev": "result", "result": {"subtype": "success", "isError": False}, "text": ""})
        await asyncio.sleep(0.05)
        self.assertEqual(len(parent_msgs()), 1)

        # Autonomous Turn 3 with real final delivery -> parent gets follow-up callback automatically!
        child_driver.items.put(assistant_item("auto:final", [{"type": "text", "text": "全部 13 种怪物已生成完毕"}]))
        await child_driver.emit({"ev": "result", "result": {"subtype": "success", "isError": False}, "text": "全部 13 种怪物已生成完毕"})
        await asyncio.sleep(0.1)
        self.assertEqual(len(parent_msgs()), 2)
        self.assertIn("后台任务完成后又更新了回复", parent_msgs()[-1])
        self.assertIn("全部 13 种怪物已生成完毕", parent_msgs()[-1])

        # 2) Async wait=False with an open background task at Turn 1 end:
        #    Turn 1 is deferred while driver.tasks is non-empty; once the background task clears and
        #    autonomous Turn 2 finishes, parent gets ONE clean callback with Turn 2's final reply.
        text2, error2 = await relay.call("subagent_send", agent_id=child_prefix, message="跑 build", wait=False)
        self.assertFalse(error2, text2)
        await asyncio.sleep(0.05)
        before_bg = len(parent_msgs())

        # Simulate a new prompt cycle whose Turn 1 ends with a background task still in driver.tasks.
        hub.subagent_watcher.prepare_prompt(child, sync_wait=False)
        child_driver.tasks["bg-build"] = {"id": "bg-build", "description": "dotnet build", "type": "tool", "background": True}
        child_driver.items.put(assistant_item("auto:bg1", [{"type": "text", "text": "等待 dotnet build 完成"}]))
        await child_driver.emit({"ev": "result", "result": {"subtype": "success", "isError": False}, "text": "等待 dotnet build 完成"})
        await asyncio.sleep(0.1)
        self.assertEqual(len(parent_msgs()), before_bg)               # deferred while bg-build is running

        # Background task finishes -> autonomous Turn 2 completes with driver.tasks empty.
        child_driver.tasks.clear()
        child_driver.items.put(assistant_item("auto:bg2", [{"type": "text", "text": "build 与单测全部通过"}]))
        await child_driver.emit({"ev": "result", "result": {"subtype": "success", "isError": False}, "text": "build 与单测全部通过"})
        await asyncio.sleep(0.1)
        self.assertEqual(len(parent_msgs()), before_bg + 1)
        self.assertIn("这一轮做完了", parent_msgs()[-1])
        self.assertIn("build 与单测全部通过", parent_msgs()[-1])

        # 3) Unexpected crash mid-turn notifies parent instead of leaving it hanging.
        hub.subagent_watcher.prepare_prompt(child, sync_wait=False)
        before_crash = len(parent_msgs())
        await child_driver.emit({"ev": "exit", "code": 137, "stderr": [], "duringTurn": True})
        await asyncio.sleep(0.1)
        self.assertEqual(len(parent_msgs()), before_crash + 1)
        self.assertIn("进程异常退出", parent_msgs()[-1])
        self.assertIn("137", parent_msgs()[-1])

    async def test_idle_reaper_does_not_kill_resuming_session_mid_start(self):
        from riesz_runner.harnesses import REAP_INTERVAL
        hub = self.app[HUB]
        me = await self.session("parent")
        await hub.harnesses.stop(me)
        # Simulate stale _active timestamp (e.g. idle-stopped 40 min ago) and slow driver.start().
        hub.harnesses._active[me] = time.time() - 3600
        gate = asyncio.Event()
        entered_start = asyncio.Event()
        orig_start = FakeDriver.start

        async def slow_start(drv: FakeDriver) -> None:
            drv.alive = True
            drv.ready = False
            drv.started_at = time.time()
            entered_start.set()
            await gate.wait()
            await orig_start(drv)

        FakeDriver.start = slow_start  # type: ignore[assignment]
        self.addCleanup(setattr, FakeDriver, "start", orig_start)

        hub.cfg = Config(workspace_root=self.tmp, claude_home=self.tmp / "claude", claude_bin="claude",
                         state_dir=self.tmp / "state", socket_path=self.socket, token=TOKEN, idle_timeout=60)
        hub.harnesses.cfg = hub.cfg
        ensure_task = asyncio.create_task(hub.harnesses.ensure(me))
        await asyncio.wait_for(entered_start.wait(), 2.0)

        # Even if a reaper tick runs right now while driver.alive=True and driver.ready=False,
        # _active was refreshed before start() and lock/ready guards prevent reaping.
        drv = hub.harnesses.drivers[me]
        self.assertFalse(drv.ready)
        self.assertTrue(hub.harnesses._locks[me].locked())
        self.assertLess(time.time() - hub.harnesses._active[me], 5.0)

        # Simulate reaper check inline with a stale timestamp to verify ready+lock guards too.
        hub.harnesses._active[me] = time.time() - 3600
        now = time.time()
        reaped = False
        for sid, d in list(hub.harnesses.drivers.items()):
            lock = hub.harnesses._locks.get(sid)
            if not d.alive or not d.ready or (lock is not None and lock.locked()) or d.busy:
                continue
            last_active = max(hub.harnesses._active.get(sid, 0.0), getattr(d, "started_at", 0.0) or 0.0)
            if last_active and now - last_active > hub.cfg.idle_timeout:
                reaped = True
        self.assertFalse(reaped)
        self.assertGreater(REAP_INTERVAL, 0)

        gate.set()
        resumed = await asyncio.wait_for(ensure_task, 2.0)
        self.assertTrue(resumed.alive and resumed.ready)

    async def test_subagent_callback_retries_transient_error_and_persists_across_restart(self):
        from riesz_runner.drivers.base import assistant_item
        from riesz_runner.process import ControlError

        hub = self.app[HUB]
        me = await self.session("parent")
        hub.harnesses.drivers[me].fake_pid = os.getpid()
        parent_driver = hub.harnesses.drivers[me]
        parent_msgs = lambda: [it["text"] for it in parent_driver.items.items if it.get("kind") == "user"]
        relay = await self.relay()

        # Start child asynchronously, then fail the first delivery.send with ControlError.
        text, error = await relay.call("subagent_start", task="长耗时任务", harness="fake", wait=False)
        self.assertFalse(error, text)
        child_prefix = text.split("子代理 ")[1][:8]
        child = next(s for s in hub.harnesses.drivers if s.startswith(child_prefix))
        child_driver = hub.harnesses.drivers[child]
        await asyncio.sleep(0.1)
        before = len(parent_msgs())

        orig_send = hub.delivery.send
        fail_once = {"remaining": 1}

        async def flaky_send(sid: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if sid == me and fail_once["remaining"] > 0:
                fail_once["remaining"] -= 1
                raise ControlError("Antigravity: context canceled")
            return await orig_send(sid, *args, **kwargs)

        hub.delivery.send = flaky_send  # type: ignore[assignment]
        self.addCleanup(setattr, hub.delivery, "send", orig_send)

        hub.subagent_watcher.prepare_prompt(child, sync_wait=False)
        child_driver.items.put(assistant_item("final:1", [{"type": "text", "text": "全部渲染完成"}]))
        await child_driver.emit({"ev": "result", "result": {"subtype": "success", "isError": False}, "text": "全部渲染完成"})
        await asyncio.sleep(0.1)

        # Must NOT be marked delivered yet; must be persisted in pending on disk!
        self.assertEqual(len(parent_msgs()), before)
        self.assertIn(child, hub.subagent_watcher._pending)
        self.assertFalse(hub.subagent_watcher._is_delivered(child, "final:1", "全部渲染完成"))
        saved = json.loads((self.cfg.state_dir / "subagent_callbacks.json").read_text(encoding="utf-8"))
        self.assertIn(child, saved["pending"])

        # Simulate runner restart: brand-new SubagentWatcher loads pending callback from disk and delivers it!
        watcher2 = tools_mod.SubagentWatcher(hub)
        self.assertIn(child, watcher2._pending)
        watcher2._pending[child]["next_at"] = 0.0
        await watcher2.flush_due()
        await asyncio.sleep(0.05)

        self.assertEqual(len(parent_msgs()), before + 1)
        self.assertIn("这一轮做完了", parent_msgs()[-1])
        self.assertIn("全部渲染完成", parent_msgs()[-1])
        self.assertNotIn(child, watcher2._pending)
        self.assertTrue(watcher2._is_delivered(child, "final:1", "全部渲染完成"))

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
        self.assertEqual(args[0::2], ["-c", "-c", "-c"])
        self.assertEqual(args[5], "mcp_servers.riesz.tool_timeout_sec=900")
        self.assertEqual(args[1], f"mcp_servers.riesz.command={json.dumps(sys.executable)}")
        self.assertEqual(json.loads(args[3].split("=", 1)[1]), list(self.server.args))

    def test_dsh(self):
        path = Path(DshDriver(cwd="/", sink=None)._tools_patch(self.server))
        text = path.read_text()
        self.assertTrue(text.startswith("- insert: "))
        [entry] = json.loads(text[len("- insert: "):])
        self.assertEqual(entry, {"id": "mcp-riesz", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "riesz", "transport": "stdio", "command": sys.executable, "args": list(self.server.args),
            "toolCallTimeoutMs": 900_000}})
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
