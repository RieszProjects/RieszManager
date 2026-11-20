"""Antigravity sessions (drivers/antigravity.py) against the scripted ACP agent, and what they may not do."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from riesz_runner.credentials import agy_models
from riesz_runner.drivers.antigravity import AntigravityDriver
from riesz_runner.drivers.jsonrpc import JsonRpcError
from riesz_runner.schedules import ScheduleError, Scheduler

from .helpers import Events
from .test_schedules import fake_hub

AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class SignInTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, auth: str | None, api: str | None = None) -> AntigravityDriver:
        env = dict(os.environ)
        if auth:
            env["FAKE_ACP_AUTH"] = auth
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=env, api=api)
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        return driver

    async def test_cached_sign_in(self):
        driver = await self.start("signed-in")
        self.assertEqual(driver.session_id, "11111111-2222-4333-8444-555555555555")
        self.assertEqual([m.id for m in driver.modes()], ["default", "yolo"])

    async def test_not_signed_in(self):
        with self.assertRaises(JsonRpcError) as caught:
            await self.start("signed-out")
        self.assertIn("agy acp --login", caught.exception.message)

    async def test_a_turn(self):
        driver = await self.start(None)
        await driver.send("hi")
        deadline = time.time() + 10
        while not driver.sink.of("result"):
            self.assertLess(time.time(), deadline)
            await asyncio.sleep(0.02)
        await driver.flush()
        texts = [b["text"] for i in driver.items.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "text"]
        self.assertEqual(texts, ["echo: hi [0 images, default]"])


class ModelsTest(unittest.TestCase):
    def test_signed_out(self):
        self.assertEqual(agy_models("Fetching available models...\nError: Please sign in to view available models."), [])

    def test_listing(self):
        out = "Fetching available models...\nAvailable models:\n  gemini-4-argon  Gemini 4 Argon (default)\n  - claude-opus-5-5\n"
        self.assertEqual([m["value"] for m in agy_models(out)], ["gemini-4-argon", "claude-opus-5-5"])


class InteractiveOnlyTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_scheduled_wake_ups(self):
        hub = fake_hub(Path(tempfile.mkdtemp()))
        hub.harnesses = type("H", (), {"harness_of": staticmethod(lambda sid: "antigravity")})()
        with self.assertRaises(ScheduleError):
            await Scheduler(hub).create({"sessionId": "6e0f5a52-0000-4000-8000-000000000001", "prompt": "x", "every": 60})


if __name__ == "__main__":
    unittest.main()
