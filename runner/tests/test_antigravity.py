"""Antigravity sessions (drivers/antigravity.py) against the scripted ACP agent."""

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

from .helpers import Events

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


class BypassTest(unittest.IsolatedAsyncioTestCase):
    """Antigravity has no mode that approves everything: Riesz Manager's rz-bypass approves for it."""

    async def start(self, mode: str | None = None) -> AntigravityDriver:
        env = {**os.environ, "FAKE_ACP_MODES": "agy"}
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=env, mode=mode)
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        return driver

    async def turn(self, driver: AntigravityDriver, text: str) -> str:
        n = len(driver.sink.of("result"))
        await driver.send(text)
        deadline = time.time() + 10
        while len(driver.sink.of("result")) == n:
            self.assertLess(time.time(), deadline, "the turn ends without anyone answering a prompt")
            await asyncio.sleep(0.02)
        await driver.flush()
        return [b["text"] for i in driver.items.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "text"][-1]

    async def test_offered_with_the_agents_own_modes(self):
        driver = await self.start()
        self.assertEqual([(m.id, m.tier) for m in driver.modes()],
                         [("default", "ask"), ("accept-edits", "edits"), ("plan", "ask"), ("rz-bypass", "full")])
        self.assertEqual(driver.modes()[2].name, "先计划")                  # the agent's own list (no auto here)

    async def test_permission_requests_are_approved_without_a_prompt(self):
        driver = await self.start()
        await driver.set_mode("rz-bypass")
        self.assertEqual(driver.mode, "rz-bypass")
        self.assertEqual(await self.turn(driver, "hi"), "echo: hi [0 images, accept-edits]")   # the agent's own most permissive
        self.assertEqual(await self.turn(driver, "edit"), "done proceed_once")
        self.assertEqual(driver.sink.of("permission"), [], "nothing was shown to the user")
        self.assertEqual(driver.mode, "rz-bypass")

    async def test_resumed_in_bypass(self):
        driver = await self.start(mode="rz-bypass")
        self.assertEqual(driver.mode, "rz-bypass")
        self.assertEqual(await self.turn(driver, "edit"), "done proceed_once")
        self.assertEqual(driver.sink.of("permission"), [])

    async def test_other_modes_still_ask(self):
        driver = await self.start()
        task = asyncio.create_task(self.turn(driver, "edit"))
        deadline = time.time() + 10
        while not driver.permissions:
            self.assertLess(time.time(), deadline)
            await asyncio.sleep(0.02)
        [request_id] = driver.permissions
        driver.answer_permission(request_id, allow=True)
        self.assertEqual(await task, "done proceed_once")


class ModelsTest(unittest.TestCase):
    def test_signed_out(self):
        self.assertEqual(agy_models("Fetching available models...\nError: Please sign in to view available models."), [])

    def test_listing(self):
        out = "Fetching available models...\nAvailable models:\n  gemini-4-argon  Gemini 4 Argon (default)\n  - claude-opus-5-5\n"
        self.assertEqual([m["value"] for m in agy_models(out)], ["gemini-4-argon", "claude-opus-5-5"])


if __name__ == "__main__":
    unittest.main()
