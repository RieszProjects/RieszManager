"""Sessions of other harnesses through the runner's own endpoints (harnesses.py, server.py), with a fake driver."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import uuid as uuidlib
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from riesz_runner import harnesses as harnesses_mod
from riesz_runner.config import Config
from riesz_runner.drivers.base import Capabilities, Driver, Mode, assistant_item, user_item
from riesz_runner.server import build_app

TOKEN = "t" * 32


class FakeDriver(Driver):
    """Answers every message with "echo: <text>"; "ask" asks a permission first, "question" asks a question."""

    harness = "fake"
    capabilities = Capabilities(steer=True, approve_for_session=True, user_questions=True)
    started = 0

    def modes(self) -> list[Mode]:
        return [Mode("ask", "问"), Mode("go", "不问")]

    async def start(self) -> None:
        FakeDriver.started += 1
        self.session_id = self.session_id or str(uuidlib.uuid4())
        self.mode = self.mode or "ask"
        self.alive = self.ready = True
        await self.emit_state()

    async def stop(self) -> None:
        self.cancel_permissions()
        self.alive = False
        await self.emit({"ev": "exit", "code": 0, "stderr": []})

    async def send(self, text: str, images: list[dict[str, Any]] | None = None, uuid: str | None = None) -> None:
        n = len(self.items.items)
        self.items.put(user_item(f"u{n}", text, uuid=uuid))
        self.turn_started()
        await self.flush()
        asyncio.get_running_loop().create_task(self._reply(text, n))

    async def _reply(self, text: str, n: int) -> None:
        answer = ""
        if text == "ask":
            got = await self.ask({"requestId": f"r{n}", "toolName": "Bash", "input": {"command": "ls"}})
            answer = f" allow={got['allow']} scope={got['scope']}"
        elif text == "question":
            got = await self.ask({"requestId": f"r{n}", "toolName": "AskUserQuestion",
                                  "input": {"questions": [{"id": "q1", "question": "哪个？", "options": [{"label": "甲"}, {"label": "乙"}]}]}})
            answer = f" answers={json.dumps(got['answers'], ensure_ascii=False)}"
        self.items.put(assistant_item(f"a{n}", [{"type": "text", "text": f"echo: {text}{answer}"}], model=self.model))
        await self.flush()
        result = self.turn_finished("success")
        await self.emit({"ev": "result", "result": result, "text": self.last_assistant_text()})
        await self.emit_state()

    async def interrupt(self) -> None:
        self.cancel_permissions()

    async def models(self) -> list[dict[str, Any]]:
        return [{"value": "fake-1", "displayName": "Fake 1"}]


class HarnessRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rz-harness-"))
        self.workspace = self.tmp / "work"
        self.workspace.mkdir()
        harnesses_mod.HARNESSES["fake"] = {"name": "Fake", "bin": "sh", "driver": FakeDriver}
        self.addCleanup(harnesses_mod.HARNESSES.pop, "fake", None)
        self.cfg = Config(workspace_root=self.tmp, claude_home=self.tmp / "claude", claude_bin="claude",
                          state_dir=self.tmp / "state", socket_path=self.tmp / "runner.sock", token=TOKEN)
        self.client = TestClient(TestServer(build_app(self.cfg)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def call(self, method: str, path: str, body: Any = None, status: int = 200) -> Any:
        resp = await self.client.request(method, f"/v1/{path}", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        data = await resp.json() if resp.content_type == "application/json" else await resp.text()
        self.assertEqual(resp.status, status, data)
        return data

    async def until(self, predicate, what: str) -> None:
        deadline = time.time() + 5
        while not await predicate():
            if time.time() > deadline:
                self.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.02)

    async def texts(self, sid: str) -> list[str]:
        items = (await self.call("GET", f"sessions/{sid}/items"))["items"]
        return [i.get("text") or "".join(b.get("text", "") for b in i.get("blocks") or ()) for i in items]

    async def new_session(self, text: str = "hi") -> str:
        res = await self.call("POST", "sessions", {"cwd": str(self.workspace), "harness": "fake", "text": text})
        self.assertEqual(res["state"]["harness"], "fake")
        return res["sessionId"]

    async def test_lists_harnesses(self):
        listed = {h["id"]: h for h in (await self.call("GET", "harnesses"))["harnesses"]}
        self.assertIn("codex", listed)
        self.assertIn("dsh", listed)
        self.assertTrue(listed["fake"]["available"])
        self.assertEqual([m["id"] for m in listed["fake"]["modes"]], ["ask", "go"])

    async def test_conversation(self):
        sid = await self.new_session()
        await self.until(lambda: self._has_text(sid, "echo: hi"), "first reply")
        sessions = (await self.call("GET", "sessions"))["sessions"]
        summary = next(s for s in sessions if s["sessionId"] == sid)
        self.assertEqual((summary["harness"], summary["promptCount"], summary["firstPrompt"]), ("fake", 1, "hi"))
        self.assertTrue(summary["live"]["hosted"])

        await self.call("POST", f"sessions/{sid}/messages", {"text": "again"})
        await self.until(lambda: self._has_text(sid, "echo: again"), "second reply")
        detail = await self.call("GET", f"sessions/{sid}")
        self.assertEqual(detail["session"]["promptCount"], 2)
        self.assertEqual([m["id"] for m in detail["state"]["modes"]], ["ask", "go"])

        await self.call("POST", f"sessions/{sid}/mode", {"mode": "go"})
        await self.call("POST", f"sessions/{sid}/mode", {"mode": "acceptEdits"}, status=400)
        await self.call("POST", f"sessions/{sid}/model", {"model": "fake-2"})
        state = (await self.call("GET", f"sessions/{sid}"))["state"]
        self.assertEqual((state["permissionMode"], state["model"]), ("go", "fake-2"))
        self.assertEqual((await self.call("GET", f"sessions/{sid}/commands"))["models"][0]["value"], "fake-1")
        await self.call("POST", f"sessions/{sid}/fork", {}, status=409)
        await self.call("POST", f"sessions/{sid}/rename", {"title": "新名字"})
        self.assertEqual((await self.call("GET", f"sessions/{sid}"))["session"]["customTitle"], "新名字")

    async def test_permission_for_the_session(self):
        sid = await self.new_session("ask")
        await self.until(lambda: self._pending(sid), "the permission request")
        rid = (await self.call("GET", f"sessions/{sid}"))["state"]["permissions"][0]["requestId"]
        await self.call("POST", f"sessions/{sid}/permissions/{rid}",
                        {"behavior": "allow", "updatedPermissions": [{"type": "session"}]})
        await self.until(lambda: self._has_text(sid, "echo: ask allow=True scope=session"), "the reply")
        await self.call("POST", f"sessions/{sid}/permissions/{rid}", {"behavior": "allow"}, status=409)

    async def test_question_answers_by_id(self):
        sid = await self.new_session("question")
        await self.until(lambda: self._pending(sid), "the question")
        rid = (await self.call("GET", f"sessions/{sid}"))["state"]["permissions"][0]["requestId"]
        await self.call("POST", f"sessions/{sid}/permissions/{rid}",
                        {"behavior": "allow", "updatedInput": {"answers": {"哪个？": "甲, 乙"}}})
        await self.until(lambda: self._has_text(sid, 'echo: question answers={"q1": ["甲", "乙"]}'), "the reply")

    async def test_stopped_session_keeps_its_conversation_and_resumes(self):
        sid = await self.new_session()
        await self.until(lambda: self._has_text(sid, "echo: hi"), "first reply")
        self.assertTrue((await self.call("POST", f"sessions/{sid}/stop"))["stopped"])
        state = (await self.call("GET", f"sessions/{sid}"))["state"]
        self.assertFalse(state["alive"])
        self.assertIn("echo: hi", await self.texts(sid))
        before = FakeDriver.started
        await self.call("POST", f"sessions/{sid}/messages", {"text": "back"})
        self.assertEqual(FakeDriver.started, before + 1)
        # The fake driver forgets on restart (a real one reads its history back).
        await self.until(lambda: self._has_text(sid, "echo: back"), "reply after resuming")

    async def test_websocket_snapshot(self):
        sid = await self.new_session()
        await self.until(lambda: self._has_text(sid, "echo: hi"), "first reply")
        ws = await self.client.ws_connect("/v1/ws", headers={"Authorization": f"Bearer {TOKEN}"})
        await ws.send_json({"op": "sub", "sessionId": sid})
        snap = json.loads((await ws.receive()).data)
        self.assertEqual((snap["ev"], snap["session"]["harness"]), ("snapshot", "fake"))
        self.assertEqual([i["kind"] for i in snap["items"]], ["user", "assistant"])
        await self.call("POST", f"sessions/{sid}/messages", {"text": "live"})
        kinds = set()
        deadline = time.time() + 5
        while not {"result", "state"} <= kinds and time.time() < deadline:
            kinds.add(json.loads((await ws.receive(timeout=5)).data)["ev"])
        self.assertTrue({"items", "state", "result"} <= kinds, kinds)
        await ws.close()

    async def test_records_survive_a_restart(self):
        sid = await self.new_session()
        await self.until(lambda: self._has_text(sid, "echo: hi"), "first reply")
        await self.client.close()
        client = TestClient(TestServer(build_app(self.cfg)))
        await client.start_server()
        self.client = client
        sessions = (await self.call("GET", "sessions"))["sessions"]
        self.assertIn(sid, [s["sessionId"] for s in sessions])
        self.assertIn("echo: hi", await self.texts(sid))

    async def test_credentials_never_come_back(self):
        from riesz_runner.server import HUB

        async def no_login(provider, bins):
            return None
        async def offline_test(provider):
            return {"ok": True, "detail": "not checked"}
        self.client.server.app[HUB].credentials.subscription = no_login
        self.client.server.app[HUB].credentials.test = offline_test
        res = await self.call("POST", "credentials/openai/set", {"key": "sk-secret-value"})
        self.assertNotIn("sk-secret-value", json.dumps(res))
        listed = await self.call("GET", "credentials")
        self.assertNotIn("sk-secret-value", json.dumps(listed))
        openai = next(p for p in listed["providers"] if p["id"] == "openai")
        self.assertEqual((openai["configured"], openai["source"]), (True, "key"))
        self.assertEqual([s["loggedIn"] for s in listed["subscriptions"]], [False, False])
        await self.call("POST", "credentials/openai/clear", {})
        openai = next(p for p in (await self.call("GET", "credentials"))["providers"] if p["id"] == "openai")
        self.assertFalse(openai["configured"])
        await self.call("POST", "credentials/nobody/set", {"key": "x"}, status=404)
        await self.call("POST", "credentials/openai/set", {}, status=400)

    async def _has_text(self, sid: str, text: str) -> bool:
        return text in await self.texts(sid)

    async def _pending(self, sid: str) -> bool:
        return bool((await self.call("GET", f"sessions/{sid}"))["state"]["permissions"])


if __name__ == "__main__":
    unittest.main()
