"""The DeepSeek Harness driver against a recorded `dsh --profile sdk` session and its session log.

The recording (tests/fixtures/dsh-session.jsonl): turn 1 writes hello.txt and
runs `cat` and `ls`; turn 2 starts `sleep 30` in the background and the process
is stopped meanwhile (then started again and the session resumed); turn 3
deletes the file; `/permission read-only`; turn 4 appends to the file, which
needs a sandbox escalation that is approved. tests/fixtures/dsh-log.jsonl is
the session log dsh wrote for it.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from riesz_runner.drivers.dsh import DshDriver, DshEvents, list_sessions, read_log

from .helpers import FIXTURES, Events, FakePeer, load_traffic

TRAFFIC = load_traffic("dsh-session.jsonl")
SESSION_ID = next(r["msg"]["params"]["sessionId"] for r in TRAFFIC
                  if r["dir"] == "out" and isinstance(r["msg"], dict) and r["msg"].get("method") == "session/query")
CWD = "/tmp/rz-fx/dsh"


def make_driver(events: Events, peer: FakePeer | None = None, home: str | None = None, **kwargs) -> DshDriver:
    driver = DshDriver(cwd=CWD, sink=events, session_id=SESSION_ID, env={"DSH_HOME": home or "/nonexistent"}, **kwargs)
    driver.peer = peer or FakePeer()
    driver.alive = driver.ready = True
    return driver


async def replay(driver: DshDriver, events: Events) -> list[dict]:
    outcomes = iter([r["msg"]["result"] for r in TRAFFIC if r["dir"] == "out" and isinstance(r["msg"], dict)
                     and isinstance(r["msg"].get("result"), dict) and "outcome" in r["msg"]["result"]])

    def answer(drv, request):
        outcome = next(outcomes)["outcome"]
        drv.answer_permission(request["requestId"], allow=outcome == "allowed-once")

    events.on_permission = answer
    pending = []
    for rec in TRAFFIC:
        msg = rec["msg"]
        if rec["dir"] != "in" or not isinstance(msg, dict) or "method" not in msg:
            continue
        if "id" in msg:
            pending.append(asyncio.create_task(driver._request(msg["method"], msg.get("params"))))
            await asyncio.sleep(0)
        else:
            await driver._notification(msg["method"], msg.get("params"))
    results = await asyncio.gather(*pending)
    await driver.flush()
    return results


def install_log(home: Path, compress: bool = False) -> Path:
    folder = home / "sessions" / "--tmp-rz-fx-dsh--" / SESSION_ID
    folder.mkdir(parents=True)
    target = folder / "session.v4.jsonl"
    shutil.copy(FIXTURES / "dsh-log.jsonl", target)
    if compress:
        subprocess.run(["zstd", "-q", "--rm", str(target), "-o", str(target) + ".zstd"], check=True)
        return Path(str(target) + ".zstd")
    return target


class ReplayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = Events()
        self.driver = make_driver(self.events)
        self.answers = await replay(self.driver, self.events)
        self.items = self.driver.items.items

    def tools(self):
        return [b for i in self.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "tool"]

    async def test_only_real_user_messages(self):
        users = [i["text"] for i in self.items if i["kind"] == "user"]
        self.assertEqual(len(users), 4)
        self.assertFalse(any("runtime context" in u or "Time sampled" in u for u in users))
        self.assertIn("hi again", users[-1])

    async def test_tools_mapped_to_known_cards(self):
        names = [t["name"] for t in self.tools()]
        self.assertEqual(names[:3], ["Write", "Bash", "Bash"])
        self.assertIn("BashOutput", names)
        self.assertEqual(self.tools()[0]["input"]["file_path"], "/tmp/rz-fx/dsh/hello.txt")
        self.assertEqual(self.tools()[1]["result"]["text"], "hi")
        self.assertEqual(self.tools()[2]["result"]["text"], "hello.txt\n")

    async def test_reasoning_and_text(self):
        thinking = [b for i in self.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "thinking"]
        texts = [b["text"] for i in self.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "text"]
        self.assertTrue(thinking)
        self.assertTrue(all(b["text"] for b in thinking))           # empty reasoning is left out
        self.assertIn("hi again", texts[-1])

    async def test_approval_carries_the_command(self):
        self.assertEqual(self.answers[-1], {"outcome": "allowed-once"})
        request = self.events.of("permission")[0]["request"]
        self.assertEqual(request["toolName"], "Bash")
        self.assertIn("hi again", request["input"]["command"])
        self.assertIn("escalate", request["reason"])

    async def test_background_job(self):
        self.assertTrue(any(t["description"].startswith("sleep 30") and t["background"] for t in self.driver.tasks.values()))
        self.assertFalse(any(t["description"] == "cat hello.txt" for t in self.driver.tasks.values()))

    async def test_permission_command(self):
        commands = [i for i in self.items if i["kind"] == "command"]
        self.assertEqual((commands[0]["name"], commands[0]["args"]), ("/permission", "read-only"))
        self.assertEqual(self.driver.mode, "read-only")

    async def test_results_and_usage(self):
        self.assertEqual(len(self.events.of("result")), 3)
        self.assertFalse(self.driver.busy)
        self.assertGreater(self.driver.context["used"], 0)
        usage = next(i["usage"] for i in self.items if i.get("usage"))
        self.assertIn("cache_read_input_tokens", usage)


class HistoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.home = Path(tempfile.mkdtemp())

    async def asyncTearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    async def check(self, compress: bool):
        install_log(self.home, compress)
        events = Events()
        driver = make_driver(events, home=str(self.home))
        self.assertIsNotNone(driver.log_path())
        await driver.load_history()
        items = driver.items.items
        self.assertEqual([i["kind"] for i in items].count("user"), 4)
        self.assertEqual([i["kind"] for i in items].count("interrupted"), 1)
        waited = [b for i in items if i["kind"] == "assistant" for b in i["blocks"]
                  if b["type"] == "tool" and b["name"] == "BashOutput"][0]
        self.assertEqual(waited["result"]["text"], "Error: wait aborted")
        self.assertEqual(events.of("reset")[0]["items"], items)
        self.assertEqual(driver.tasks, {})                 # history does not revive jobs

    async def test_plain_log(self):
        await self.check(False)

    @unittest.skipUnless(shutil.which("zstd"), "zstd is not installed")
    async def test_compressed_log(self):
        await self.check(True)

    async def test_lists_sessions_by_folder(self):
        install_log(self.home)
        found = list_sessions(CWD, env={"DSH_HOME": str(self.home)})
        self.assertEqual([s["id"] for s in found], [SESSION_ID])
        self.assertEqual(list_sessions("/elsewhere", env={"DSH_HOME": str(self.home)}), [])

    async def test_read_log_skips_damage(self):
        path = self.home / "broken.jsonl"
        path.write_text('{"type":"session"}\nnot json\n{"type":"turn/start","seq":1,"data":{"turn":1}}\n')
        self.assertEqual([e["type"] for e in await read_log(path)], ["session", "turn/start"])


class SendTest(unittest.IsolatedAsyncioTestCase):
    async def test_mode_is_set_before_the_first_message(self):
        peer = FakePeer({"session/query": {"kind": "prompt", "messageId": "m1"}})
        driver = make_driver(Events(), peer, mode="read-only")
        await driver.send("hi", uuid="u1")
        texts = [p["contentBlocks"][0]["text"] for p in peer.sent("session/query")]
        self.assertEqual(texts, ["/permission read-only", "hi"])
        await driver.send("again")
        self.assertEqual(len(peer.sent("session/query")), 3)

    async def test_uuid_reaches_the_user_item(self):
        peer = FakePeer({"session/query": {"kind": "prompt", "messageId": "m1"}})
        events = Events()
        driver = make_driver(events, peer)
        await driver.send("hi", uuid="u1")
        await driver._notification("session.event", {"sessionId": SESSION_ID, "event": {
            "type": "user/message", "seq": 5, "time": 0, "data": {"id": "m1", "source": {"kind": "user"}, "content": [{"type": "text", "text": "hi"}]}}})
        self.assertEqual(driver.items.get("d:u:m1")["uuid"], "u1")

    async def test_images_are_inlined(self):
        peer = FakePeer({"session/query": {"kind": "prompt", "messageId": "m1"}})
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(b"\x89PNG fake")
            f.flush()
            await make_driver(Events(), peer).send("look", images=[{"path": f.name, "mime": "image/png"}])
        blocks = peer.sent("session/query")[0]["contentBlocks"]
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["mimeType"], "image/png")

    async def test_other_sessions_are_ignored(self):
        driver = make_driver(Events())
        await driver._notification("session.status", {"sessionId": "someone-else", "status": "running"})
        self.assertFalse(driver.busy)

    async def test_user_question_answers(self):
        events = Events()
        events.on_permission = lambda drv, req: drv.answer_permission(req["requestId"], allow=True, answers={"q1": ["A", "something else"]})
        driver = make_driver(events)
        res = await driver._request("user-question/ask", {"requestId": "r1", "sessionId": SESSION_ID, "questions": [
            {"id": "q1", "question": "Which?", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": True}]})
        self.assertEqual(res, {"answers": [{"id": "q1", "selected": ["A"], "custom": "something else"}]})

    async def test_question_cancelled_by_dsh(self):
        driver = make_driver(Events())
        ask = asyncio.create_task(driver._request("user-question/ask", {"requestId": "r2", "sessionId": SESSION_ID,
                                                                          "questions": [{"id": "q", "question": "?"}]}))
        await asyncio.sleep(0.01)
        await driver._notification("user-question.cancelled", {"requestId": "r2", "sessionId": SESSION_ID})
        self.assertEqual(await ask, {"cancelled": True})

    async def test_rejected_and_cancelled_approvals(self):
        events = Events()
        events.on_permission = lambda drv, req: drv.answer_permission(req["requestId"], allow=False)
        driver = make_driver(events)
        self.assertEqual(await driver._request("approval/request", {"toolName": "bash", "callId": "c"}), {"outcome": "rejected"})
        events.on_permission = None
        ask = asyncio.create_task(driver._request("approval/request", {"toolName": "bash"}))
        await asyncio.sleep(0.01)
        driver.cancel_permissions()
        self.assertEqual(await ask, {"outcome": "cancelled"})

    async def test_settings_wait_for_idle(self):
        driver = make_driver(Events())
        restarts = []

        async def fake_restart():
            restarts.append(driver.model)
        driver.restart = fake_restart
        driver.busy = True
        await driver.set_model("deepseek-flash")
        self.assertEqual(restarts, [])
        self.assertTrue(driver._restart_pending)
        await driver._notification("session.status", {"sessionId": SESSION_ID, "status": "idle"})
        self.assertEqual(restarts, ["deepseek-flash"])
        with self.assertRaises(ValueError):
            await driver.set_effort("xhigh")

    async def test_interrupt_restarts_and_rereads(self):
        home = Path(tempfile.mkdtemp())
        try:
            install_log(home)
            events = Events()
            driver = make_driver(events, home=str(home))
            spawned = []

            async def fake_spawn(*, resume):
                spawned.append(resume)
            driver._spawn = fake_spawn
            driver.busy = True
            await driver.interrupt()
            self.assertEqual(spawned, [True])
            self.assertTrue(driver.peer.stopped)
            self.assertFalse(driver.busy)
            self.assertEqual(events.of("result")[-1]["result"]["subtype"], "interrupted")
            self.assertTrue(events.of("reset"))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    async def test_models_without_a_key(self):
        driver = make_driver(Events())
        models = await driver.models()
        self.assertEqual(models[0]["value"], "deepseek-v4-pro")


class EventsTest(unittest.TestCase):
    def test_failed_turn_is_a_notice(self):
        driver = DshDriver(cwd=CWD, sink=Events(), session_id="s")
        DshEvents(driver).apply({"type": "turn/end", "data": {"turn": 7, "reason": {"kind": "failed", "message": "model refused"}}}, live=False)
        item = driver.items.get("d:t:7:end")
        self.assertEqual(item["kind"], "notice")
        self.assertIn("model refused", item["text"])


if __name__ == "__main__":
    unittest.main()
