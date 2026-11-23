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


class BackgroundTest(unittest.IsolatedAsyncioTestCase):
    """A call still running when the turn ends is a background task until it reports, not a cut-off call."""

    async def test_call_finishing_after_the_turn(self):
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=dict(os.environ))
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        await driver.send("bg")
        deadline = time.time() + 10
        while not driver.sink.of("result"):
            self.assertLess(time.time(), deadline)
            await asyncio.sleep(0.02)
        self.assertEqual([(t["id"], t["background"], t["description"]) for t in driver.snapshot()["tasks"]], [("t3", True, "make paper")])
        self.assertFalse(driver.busy)
        while driver.tasks:
            self.assertLess(time.time(), deadline, "the task goes when the call reports")
            await asyncio.sleep(0.02)
        await driver.flush()
        [block] = [b for i in driver.items.items for b in i.get("blocks") or () if b.get("type") == "tool"]
        self.assertEqual(block["result"]["text"], "built")
        self.assertIn("paper built", driver.last_assistant_text())


class AutonomousTest(unittest.IsolatedAsyncioTestCase):
    """Work Antigravity starts by itself is a turn: working until its updates stop, then a result."""

    async def until(self, check, what: str, seconds: float = 10) -> None:
        deadline = time.time() + seconds
        while not check():
            self.assertLess(time.time(), deadline, what)
            await asyncio.sleep(0.02)

    async def test_own_timer_then_waking_up(self):
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=dict(os.environ))
        driver.autonomous_quiet = 0.5
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        await driver.send("timer")
        await self.until(lambda: driver.sink.of("result"), "the turn ends")
        self.assertFalse(driver.busy)
        self.assertEqual([t["description"] for t in driver.tasks.values()], ["1 秒后自己接着做"])   # its timer, until it fires
        await self.until(lambda: driver.busy, "it wakes up and works: shown as working")
        await self.until(lambda: len(driver.sink.of("result")) == 2, "its own turn ends once it is quiet")
        self.assertFalse(driver.busy)
        self.assertEqual(driver.tasks, {}, "the timer fired: no longer a background task")
        self.assertIn("checked again", driver.last_assistant_text())

    async def test_message_while_it_works_on_its_own_waits_for_it(self):
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=dict(os.environ))
        driver.autonomous_quiet = 0.5
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        await driver.send("timer")
        await self.until(lambda: driver.busy and len(driver.sink.of("result")) == 1 and driver._autonomous, "working on its own")
        await driver.send("hello")
        self.assertEqual([q["preview"] for q in driver.snapshot()["queue"]], ["hello"])      # shown as queued
        await self.until(lambda: len(driver.sink.of("result")) == 3, "then answered")
        self.assertIn("echo: hello", driver.last_assistant_text())

    async def test_autonomous_permission_prompt_does_not_timeout(self):
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=dict(os.environ))
        driver.autonomous_quiet = 0.35
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        await driver.send("timer_ask")
        await self.until(lambda: bool(driver.permissions), "autonomous turn prompts user")
        # Wait longer than autonomous_quiet before answering: the turn must stay alive waiting for the user.
        await asyncio.sleep(0.6)
        self.assertTrue(driver.busy)
        self.assertTrue(driver.permissions)
        self.assertEqual(len(driver.sink.of("result")), 1)
        [req_id] = driver.permissions
        driver.answer_permission(req_id, allow=True)
        await self.until(lambda: len(driver.sink.of("result")) == 2, "autonomous turn finishes after approval")
        self.assertIn("autonomous proceed_once", driver.last_assistant_text())

    async def test_interrupt_ends_autonomous_turn_promptly(self):
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=dict(os.environ))
        driver.autonomous_quiet = 5.0
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        await driver.send("timer_ask")
        await self.until(lambda: driver.busy and driver._autonomous and bool(driver.permissions), "autonomous turn active")
        t0 = time.monotonic()
        await driver.interrupt()
        await self.until(lambda: len(driver.sink.of("result")) == 2 and not driver.busy, "interrupted promptly")
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertTrue(any(it.get("kind") == "interrupted" for it in driver.items.items))


class LateReplayTest(unittest.IsolatedAsyncioTestCase):
    """Antigravity answers session/load before replaying the history: the next message still goes after it."""

    async def test_message_sent_right_after_resuming(self):
        env = {**os.environ, "FAKE_ACP_LATE_REPLAY": "1"}
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=env,
                                   session_id="11111111-2222-4333-8444-555555555555")
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        await driver.send("now")
        deadline = time.time() + 10
        while not driver.sink.of("result"):
            self.assertLess(time.time(), deadline)
            await asyncio.sleep(0.02)
        await driver.flush()
        shown = [(i["kind"], i.get("text") or " ".join(b["text"] for b in i.get("blocks") or () if b.get("type") == "text"))
                 for i in driver.items.items]
        self.assertEqual(shown, [("user", "earlier question"), ("assistant", "earlier answer"),
                                 ("user", "now"), ("assistant", "echo: now [0 images, default]")])


class ElicitationTest(unittest.IsolatedAsyncioTestCase):
    """Antigravity ask_question arrives over ACP elicitation/create and always asks the user (even in rz-bypass)."""

    async def test_ask_question_elicitation_in_bypass_mode(self):
        env = {**os.environ, "FAKE_ACP_MODES": "agy"}
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=env, mode="rz-bypass")
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        self.assertTrue(driver.capabilities.user_questions)
        await driver.send("question")
        deadline = time.time() + 10
        while not driver.permissions:
            self.assertLess(time.time(), deadline, "elicitation/create must prompt the user even in rz-bypass")
            await asyncio.sleep(0.02)
        [request_id] = driver.permissions
        req = driver.permissions[request_id].entry
        self.assertEqual(req["toolName"], "AskUserQuestion")
        qs = req["input"]["questions"]
        self.assertEqual([q["id"] for q in qs], ["q1", "q2"])
        self.assertEqual([o["label"] for o in qs[0]["options"]], ["Red, warm", "Blue"])
        self.assertFalse(qs[0]["multiSelect"])
        self.assertTrue(qs[1]["multiSelect"])
        # Simulate frontend comma-joined answer via harnesses._split_question_answers
        from riesz_runner.harnesses import _split_question_answers
        a1 = _split_question_answers("Red, warm", qs[0]["options"])
        a2 = _split_question_answers("Tea, custom drink", qs[1]["options"])
        self.assertEqual(a1, ["Red, warm"])
        self.assertEqual(a2, ["Tea", "custom drink"])
        driver.answer_permission(request_id, allow=True, answers={"q1": a1, "q2": a2})
        while not driver.sink.of("result"):
            self.assertLess(time.time(), deadline)
            await asyncio.sleep(0.02)
        await driver.flush()
        self.assertEqual(driver.tasks, {}, "ask_question must not become a fake background task")
        reply = driver.last_assistant_text()
        self.assertIn('"action": "accept"', reply)
        self.assertIn('"q1": "1"', reply)
        self.assertIn('"q2": ["1"]', reply)
        self.assertIn('"q2_other": "custom drink"', reply)
        tool = next(b for i in driver.items.items for b in i.get("blocks") or () if b.get("type") == "tool")
        self.assertEqual(tool["name"], "AskUserQuestion")
        self.assertEqual(tool["result"]["structured"]["answers"], {"Pick color": "Red, warm", "Pick drinks": "Tea, custom drink"})

    async def test_ask_question_declined(self):
        env = {**os.environ, "FAKE_ACP_MODES": "agy"}
        driver = AntigravityDriver(agy=AGENT, cwd=tempfile.mkdtemp(), sink=Events(), env=env, mode="rz-bypass")
        self.addAsyncCleanup(driver.stop)
        await driver.start()
        await driver.send("question")
        deadline = time.time() + 10
        while not driver.permissions:
            self.assertLess(time.time(), deadline)
            await asyncio.sleep(0.02)
        [request_id] = driver.permissions
        driver.answer_permission(request_id, allow=False)
        while not driver.sink.of("result"):
            self.assertLess(time.time(), deadline)
            await asyncio.sleep(0.02)
        await driver.flush()
        self.assertIn('"action": "decline"', driver.last_assistant_text())


class ModelsTest(unittest.TestCase):
    def test_signed_out(self):
        self.assertEqual(agy_models("Fetching available models...\nError: Please sign in to view available models."), [])

    def test_listing(self):
        out = "Fetching available models...\nAvailable models:\n  gemini-4-argon  Gemini 4 Argon (default)\n  - claude-opus-5-5\n"
        self.assertEqual([m["value"] for m in agy_models(out)], ["gemini-4-argon", "claude-opus-5-5"])


if __name__ == "__main__":
    unittest.main()
