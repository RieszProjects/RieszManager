"""End-to-end: the drivers against the real harnesses. Skipped unless asked for:

    RZ_LIVE=1 python -m unittest tests.test_live                       # Codex (needs `codex login`)
    RZ_LIVE=1 RZ_DSH_KEYFILE=/path/to/key python -m unittest tests.test_live   # also dsh

Each run uses a fresh scratch folder and a few short turns of the cheapest
settings, so it costs a little of the accounts' usage.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from riesz_runner.drivers.codex import CodexDriver
from riesz_runner.drivers.dsh import DshDriver

from tests.helpers import Events

LIVE = os.environ.get("RZ_LIVE") == "1"
KEYFILE = os.environ.get("RZ_DSH_KEYFILE")


class Live:
    """Helpers shared by the live tests."""

    events: Events
    driver: object

    async def until_result(self, count: int, timeout: float = 240.0) -> dict:
        deadline = time.time() + timeout
        while len(self.events.of("result")) < count:
            if time.time() > deadline:
                self.fail(f"no result #{count} within {timeout}s; items: {[i['kind'] for i in self.driver.items.items]}")
            await asyncio.sleep(0.25)
        return self.events.of("result")[count - 1]["result"]

    async def until(self, predicate, timeout: float = 120.0, what: str = "condition") -> None:
        deadline = time.time() + timeout
        while not predicate():
            if time.time() > deadline:
                self.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.25)

    def tools(self) -> list[dict]:
        return [b for i in self.driver.items.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "tool"]

    def scratch(self) -> str:
        folder = tempfile.mkdtemp(prefix="rz-live-")
        subprocess.run(["git", "init", "-q", folder], check=True)
        self.addCleanup(shutil.rmtree, folder, True)
        return folder


@unittest.skipUnless(LIVE and shutil.which("codex"), "set RZ_LIVE=1 (and log in to Codex) to run")
class CodexLive(Live, unittest.IsolatedAsyncioTestCase):
    async def test_conversation(self):
        cwd = self.scratch()
        self.events = Events()
        self.events.on_permission = lambda drv, req: drv.answer_permission(req["requestId"], allow=True)
        self.driver = d = CodexDriver(cwd=cwd, sink=self.events, effort="low")
        await d.start()
        self.addAsyncCleanup(d.stop)
        self.assertTrue(d.session_id)
        self.assertTrue(await d.models())
        self.assertIsNotNone(await d.read_rate_limits())

        await d.send("运行 `echo 1 > a.txt && cat a.txt`，然后只回复「好」。")
        self.assertEqual((await self.until_result(1))["subtype"], "success")
        self.assertTrue(any(t["name"] == "Bash" and "1" in (t["result"] or {}).get("text", "") for t in self.tools()))

        await d.set_mode("read-only")
        await d.send("把数字 2 写进 b.txt（需要的话请求权限），然后只回复「好」。")
        await self.until_result(2)
        self.assertTrue(self.events.of("permission"), "writing in read-only mode should ask")
        self.assertEqual(Path(cwd, "b.txt").read_text().strip(), "2")

        await d.set_mode("full-access")
        await d.send("运行 `sleep 60`，结束后告诉我。")
        await self.until(lambda: any(t["input"].get("command", "").startswith("sleep") for t in self.tools()), what="sleep to start")
        await asyncio.sleep(1.0)
        await d.interrupt()
        self.assertEqual((await self.until_result(3, 60))["subtype"], "interrupted")
        self.assertIn("interrupted", [i["kind"] for i in d.items.items])

        thread = d.session_id
        await d.stop()
        events2 = Events()
        d2 = CodexDriver(cwd=cwd, sink=events2, session_id=thread)
        await d2.start()
        self.addAsyncCleanup(d2.stop)
        users = [i["text"] for i in d2.items.items if i["kind"] == "user"]
        self.assertEqual(len(users), 3)
        self.assertTrue(events2.of("reset"))


@unittest.skipUnless(LIVE and KEYFILE and shutil.which("dsh"), "set RZ_LIVE=1 and RZ_DSH_KEYFILE to run")
class DshLive(Live, unittest.IsolatedAsyncioTestCase):
    def env(self) -> dict:
        return dict(os.environ, DEEPSEEK_API_KEY=Path(KEYFILE).read_text().strip())

    async def test_conversation(self):
        cwd = self.scratch()
        self.events = Events()
        self.events.on_permission = lambda drv, req: drv.answer_permission(req["requestId"], allow=True)
        self.driver = d = DshDriver(cwd=cwd, sink=self.events, env=self.env(), model="deepseek-flash", effort="low")
        await d.start()
        self.addAsyncCleanup(d.stop)
        models = await d.models()
        self.assertTrue(any(m["value"] == "deepseek-flash" for m in models))

        await d.send("运行 `echo 1 > a.txt && cat a.txt`，然后只回复「好」。", uuid="first")
        self.assertEqual((await self.until_result(1))["subtype"], "success")
        self.assertEqual(next(i for i in d.items.items if i["kind"] == "user")["uuid"], "first")
        self.assertTrue(any(t["name"] == "Bash" and "1" in (t["result"] or {}).get("text", "") for t in self.tools()))

        await d.send("在前台运行 `sleep 60`（不要放到后台），结束后告诉我。")
        await self.until(lambda: any("sleep" in t["input"].get("command", "") for t in self.tools()), what="sleep to start")
        await asyncio.sleep(2.0)
        await d.interrupt()
        self.assertEqual((await self.until_result(2, 90))["subtype"], "interrupted")
        self.assertTrue(d.alive)
        self.assertIn("interrupted", [i["kind"] for i in d.items.items])

        await d.send("只回复「还在」。")
        self.assertEqual((await self.until_result(3))["subtype"], "success")

        session = d.session_id
        await d.stop()
        events2 = Events()
        d2 = DshDriver(cwd=cwd, sink=events2, env=self.env(), session_id=session, model="deepseek-flash", effort="low")
        await d2.start()
        self.addAsyncCleanup(d2.stop)
        users = [i["text"] for i in d2.items.items if i["kind"] == "user"]
        self.assertEqual(len(users), 3)
        await d2.send("只回复「恢复了」。")
        deadline = time.time() + 120
        while not events2.of("result") and time.time() < deadline:
            await asyncio.sleep(0.25)
        self.assertTrue(events2.of("result"))


if __name__ == "__main__":
    unittest.main()
