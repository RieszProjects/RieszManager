"""The ACP driver (drivers/acp.py) as Gemini CLI's, against a scripted agent (fake_acp_agent.py)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from riesz_runner.drivers.acp import tool_input
from riesz_runner.drivers.gemini import GeminiDriver, settings_file

from .helpers import Events

AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class ToolInputTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(tool_input({"kind": "execute", "title": "ls -la"}), ("Bash", {"command": "ls -la", "description": None}))
        name, inp = tool_input({"kind": "edit", "content": [{"type": "diff", "path": "/a", "oldText": "x", "newText": "y"}]})
        self.assertEqual((name, inp), ("Edit", {"file_path": "/a", "old_string": "x", "new_string": "y"}))
        self.assertEqual(tool_input({"kind": "edit", "content": [{"type": "diff", "path": "/b", "oldText": None, "newText": "new"}]}),
                         ("Write", {"file_path": "/b", "content": "new"}))
        self.assertEqual(tool_input({"kind": "read", "title": "ReadFile", "locations": [{"path": "/c"}]})[0], "Read")
        self.assertEqual(tool_input({"kind": "other", "title": "GoogleSearch"}), ("GoogleSearch", {"title": "GoogleSearch"}))


class GeminiDriverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events = Events()
        self.events.on_permission = None
        self.tmp = tempfile.mkdtemp(prefix="rz-gem-")
        self.driver = GeminiDriver(gemini=AGENT, cwd=self.tmp, sink=self.events, env=dict(os.environ))
        await self.driver.start()
        self.addAsyncCleanup(self.driver.stop)

    async def results(self, n: int) -> list[dict]:
        deadline = time.time() + 10
        while len(self.events.of("result")) < n:
            self.assertLess(time.time(), deadline, "no result")
            await asyncio.sleep(0.02)
        await self.driver.flush()
        return [e["result"] for e in self.events.of("result")]

    def texts(self) -> list[str]:
        return [b["text"] for i in self.driver.items.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "text"]

    async def test_session_modes_and_models(self):
        d = self.driver
        self.assertEqual(d.session_id, "11111111-2222-4333-8444-555555555555")
        self.assertEqual([m.id for m in d.modes()], ["default", "yolo"])
        self.assertEqual(d.modes()[0].name, "逐项确认")        # Gemini's mode ids get the web app's names
        self.assertEqual([m["value"] for m in await d.models()], ["gemini-pro", "gemini-flash"])
        self.assertEqual(d.model, "gemini-pro")

    async def test_a_turn(self):
        image = Path(self.tmp) / "a.png"
        image.write_bytes(b"\x89PNG fake")
        await self.driver.send("hi", [{"path": str(image), "mime": "image/png"}], uuid="u1")
        result = (await self.results(1))[0]
        self.assertEqual(result["subtype"], "success")
        self.assertEqual(self.texts(), ["echo: hi [1 images, default]"])
        thinking = [b for i in self.driver.items.items for b in i.get("blocks") or () if b["type"] == "thinking"]
        self.assertEqual(thinking[0]["text"], "thinking…")
        user = next(i for i in self.driver.items.items if i["kind"] == "user")
        self.assertEqual((user["text"], user["uuid"]), ("hi", "u1"))
        self.assertEqual(self.driver.context, {"used": 1200, "window": 1000000})
        self.assertFalse(self.driver.busy)

    async def test_permission_for_the_session(self):
        def allow(driver, request):
            self.assertEqual(request["toolName"], "Edit")
            self.assertEqual(request["input"]["file_path"], "/w/notes.txt")
            driver.answer_permission(request["requestId"], allow=True, scope="session")
        self.events.on_permission = allow
        await self.driver.send("edit")
        await self.results(1)
        self.assertEqual(self.texts(), ["done proceed_always"])
        tool = next(b for i in self.driver.items.items for b in i.get("blocks") or () if b["type"] == "tool")
        self.assertEqual((tool["name"], tool["input"]["new_string"], tool["result"]["text"]), ("Edit", "b\n", "chose proceed_always"))

    async def test_denied(self):
        self.events.on_permission = lambda driver, req: driver.answer_permission(req["requestId"], allow=False)
        await self.driver.send("edit")
        await self.results(1)
        tool = next(b for i in self.driver.items.items for b in i.get("blocks") or () if b["type"] == "tool")
        self.assertTrue(tool["result"]["isError"])

    async def test_interrupt_and_queue(self):
        await self.driver.send("wait")
        await asyncio.sleep(0.3)
        await self.driver.send("after", uuid="q1")
        self.assertEqual([q["uuid"] for q in self.driver.snapshot()["queue"]], ["q1"])
        await self.driver.interrupt()
        results = await self.results(2)
        self.assertEqual([r["subtype"] for r in results], ["interrupted", "success"])
        kinds = [i["kind"] for i in self.driver.items.items]
        self.assertIn("interrupted", kinds)
        self.assertEqual(self.texts()[-1], "echo: after [0 images, default]")
        self.assertEqual(self.driver.snapshot()["queue"], [])

    async def test_mode_change_and_resume(self):
        await self.driver.set_mode("yolo")
        await self.driver.send("x")
        await self.results(1)
        self.assertEqual(self.texts(), ["echo: x [0 images, yolo]"])
        sid = self.driver.session_id
        await self.driver.stop()
        events = Events()
        again = GeminiDriver(gemini=AGENT, cwd=self.tmp, sink=events, env=dict(os.environ), session_id=sid, mode="yolo")
        await again.start()
        self.addAsyncCleanup(again.stop)
        self.assertEqual([(i["kind"], i.get("text")) for i in again.items.items][:1], [("user", "earlier question")])
        self.assertTrue(events.of("reset"))
        self.assertEqual(again.mode, "yolo")

    def test_settings_file(self):
        path = settings_file(Path(self.tmp))
        data = json.loads(Path(path).read_text())
        self.assertEqual(data["security"]["auth"]["selectedType"], "gemini-api-key")
        self.assertFalse(data["privacy"]["usageStatisticsEnabled"])
        self.assertTrue(data["security"]["environmentVariableRedaction"]["enabled"])


if __name__ == "__main__":
    unittest.main()
