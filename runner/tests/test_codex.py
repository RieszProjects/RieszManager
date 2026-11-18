"""The Codex driver against a recorded `codex app-server` session (tests/fixtures/codex-session.jsonl).

The recording: turn 1 creates hello.txt and runs commands (approved), turn 2
starts `sleep 30` and is interrupted, turn 3 asks to delete the file and the
command is declined; then the history is read back.
"""

from __future__ import annotations

import asyncio
import unittest

from riesz_runner.drivers.codex import CodexDriver, map_item, unwrap_command
from riesz_runner.drivers.jsonrpc import JsonRpcError

from .helpers import Events, FakePeer, load_traffic, responses_by_method

TRAFFIC = load_traffic("codex-session.jsonl")
RESPONSES = responses_by_method(TRAFFIC)
THREAD_ID = RESPONSES["thread/start"][0]["thread"]["id"]


def make_driver(events: Events, peer: FakePeer | None = None, **kwargs) -> CodexDriver:
    driver = CodexDriver(cwd="/tmp/rz-fx/codex", sink=events, **kwargs)
    driver.peer = peer or FakePeer()
    driver.session_id = THREAD_ID
    driver.alive = driver.ready = True
    return driver


async def replay(driver: CodexDriver, events: Events) -> list[str]:
    """Feed the recorded notifications and requests to the driver; answer prompts as the recording did."""
    decisions = [r["msg"]["result"]["decision"] for r in TRAFFIC
                 if r["dir"] == "out" and isinstance(r["msg"], dict) and "result" in r["msg"] and "decision" in (r["msg"]["result"] or {})]
    answers = iter(decisions)
    given: list[str] = []

    def answer(drv, request):
        decision = next(answers)
        given.append(decision)
        drv.answer_permission(request["requestId"], allow=decision.startswith("accept"),
                              scope="session" if decision == "acceptForSession" else "once")

    events.on_permission = answer
    pending = []
    for rec in TRAFFIC:
        msg = rec["msg"]
        if rec["dir"] != "in" or not isinstance(msg, dict) or "method" not in msg:
            continue
        if "id" in msg:                                   # a request from Codex: answered via the sink
            pending.append(asyncio.create_task(driver._request(msg["method"], msg.get("params"))))
            await asyncio.sleep(0)
        else:
            if msg["method"] == "turn/started":
                driver.turn_started()
            await driver._notification(msg["method"], msg.get("params"))
    results = await asyncio.gather(*pending)
    await driver.flush()
    return [r["decision"] for r in results]


class MapItemTest(unittest.TestCase):
    def test_unwraps_login_shell(self):
        self.assertEqual(unwrap_command("/bin/bash -lc 'sleep 30 && echo done'"), "sleep 30 && echo done")
        # Inside double quotes the shell turns \\\\ into \\: the command Codex really ran.
        self.assertEqual(unwrap_command("/bin/bash -lc \"printf '%s\\\\n' hi > hello.txt\""), "printf '%s\\n' hi > hello.txt")
        self.assertEqual(unwrap_command("ls -la"), "ls -la")
        self.assertEqual(unwrap_command("bash -lc 'unterminated"), "bash -lc 'unterminated")

    def test_command_states(self):
        running = map_item({"type": "commandExecution", "id": "x", "command": "ls", "status": "inProgress"})
        self.assertIsNone(running["blocks"][0]["result"])
        done = map_item({"type": "commandExecution", "id": "x", "command": "ls", "status": "completed", "exitCode": 0, "aggregatedOutput": "a\n"})
        self.assertEqual(done["blocks"][0]["result"]["text"], "a\n")
        self.assertFalse(done["blocks"][0]["result"]["isError"])
        failed = map_item({"type": "commandExecution", "id": "x", "command": "false", "status": "completed", "exitCode": 1})
        self.assertTrue(failed["blocks"][0]["result"]["isError"])

    def test_empty_reasoning_is_redacted_thinking(self):
        item = map_item({"type": "reasoning", "id": "r", "summary": [], "content": []})
        self.assertEqual(item["blocks"][0]["type"], "thinking")
        self.assertTrue(item["blocks"][0]["redacted"])

    def test_unknown_kinds_become_generic_tools(self):
        item = map_item({"type": "somethingNew", "id": "n", "status": "completed", "detail": 1})
        self.assertEqual(item["blocks"][0]["name"], "somethingNew")
        self.assertEqual(item["blocks"][0]["input"], {"status": "completed", "detail": 1})


class ReplayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = Events()
        self.driver = make_driver(self.events)
        self.decisions = await replay(self.driver, self.events)
        self.items = self.driver.items.items

    def users(self):
        return [i["text"] for i in self.items if i["kind"] == "user"]

    def tools(self):
        return [b for i in self.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "tool"]

    async def test_user_messages_in_order(self):
        self.assertEqual(len(self.users()), 3)
        self.assertIn("hello.txt", self.users()[0])
        self.assertIn("sleep 30", self.users()[1])
        self.assertIn("删除", self.users()[2])

    async def test_commands_and_their_outcomes(self):
        tools = self.tools()
        self.assertEqual([t["name"] for t in tools], ["Bash", "Bash", "Bash"])
        ok, interrupted, declined = tools
        self.assertIn("cat hello.txt", ok["input"]["command"])
        self.assertNotIn("/bin/bash", ok["input"]["command"])
        self.assertEqual(ok["result"]["text"], "hi\nhello.txt\n")
        self.assertFalse(ok["result"]["isError"])
        self.assertTrue(interrupted["result"]["structured"]["interrupted"])
        self.assertEqual(declined["result"]["structured"]["status"], "declined")
        self.assertTrue(declined["result"]["isError"])

    async def test_interruption_is_shown(self):
        kinds = [i["kind"] for i in self.items]
        self.assertIn("interrupted", kinds)
        self.assertLess(kinds.index("interrupted"), max(i for i, k in enumerate(kinds) if k == "user"))

    async def test_final_reply_text(self):
        texts = [b["text"] for i in self.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "text"]
        self.assertTrue(any("hello.txt" in t for t in texts))
        self.assertIn("拒绝", texts[-1])

    async def test_permissions_answered_as_recorded(self):
        self.assertEqual(self.decisions, ["accept", "accept", "decline"])
        asked = self.events.of("permission")
        self.assertEqual(len(asked), 3)
        self.assertTrue(all(a["request"]["toolName"] == "Bash" for a in asked))
        self.assertEqual(len(self.events.of("permission_resolved")), 3)
        self.assertEqual(self.driver.permissions, {})

    async def test_turn_results(self):
        results = [e["result"]["subtype"] for e in self.events.of("result")]
        self.assertEqual(results, ["success", "interrupted", "success"])
        self.assertFalse(self.driver.busy)

    async def test_streamed_text_matches_final(self):
        # Deltas arrive before item/completed; the completed text must not be duplicated.
        texts = [b["text"] for i in self.items if i["kind"] == "assistant" for b in i["blocks"] if b["type"] == "text"]
        self.assertFalse(any(t and t[: len(t) // 2] * 2 == t for t in texts))

    async def test_context_usage(self):
        self.assertIsNotNone(self.driver.context)
        self.assertGreater(self.driver.context["used"], 0)

    async def test_state_snapshot_shape(self):
        state = self.driver.snapshot()
        for key in ("hosted", "alive", "busy", "model", "permissionMode", "effort", "queue", "permissions", "tasks", "lastResult"):
            self.assertIn(key, state)
        self.assertEqual(state["harness"], "codex")
        self.assertTrue(state["capabilities"]["steer"])


class HistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_rebuilds_from_turns(self):
        turns = RESPONSES["thread/turns/list"][0]["data"]            # recorded newest first
        events = Events()
        peer = FakePeer({"thread/turns/list": {"data": list(reversed(turns)), "nextCursor": None}})
        driver = make_driver(events, peer)
        await driver.load_history()
        kinds = [i["kind"] for i in driver.items.items]
        self.assertEqual(kinds.count("user"), 3)
        self.assertEqual(kinds.count("interrupted"), 1)
        self.assertEqual(peer.sent("thread/turns/list")[0]["sortDirection"], "asc")
        self.assertEqual(events.of("reset")[0]["items"], driver.items.items)

    async def test_pages_through_history(self):
        turns = list(reversed(RESPONSES["thread/turns/list"][0]["data"]))
        pages = iter([{"data": turns[:1], "nextCursor": "c1"}, {"data": turns[1:], "nextCursor": None}])
        peer = FakePeer({"thread/turns/list": lambda params: next(pages)})
        driver = make_driver(Events(), peer)
        await driver.load_history()
        self.assertEqual([p.get("cursor") for p in peer.sent("thread/turns/list")], [None, "c1"])
        self.assertEqual([i["kind"] for i in driver.items.items].count("user"), 3)


class SendTest(unittest.IsolatedAsyncioTestCase):
    async def test_idle_send_starts_a_turn_with_the_mode(self):
        peer = FakePeer({"turn/start": {"turn": {"id": "t1"}}})
        driver = make_driver(Events(), peer, model="gpt-x", effort="high", mode="read-only")
        await driver.send("hi", uuid="u1")
        params = peer.sent("turn/start")[0]
        self.assertEqual(params["input"], [{"type": "text", "text": "hi", "text_elements": []}])
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual((params["model"], params["effort"], params["clientUserMessageId"]), ("gpt-x", "high", "u1"))
        self.assertTrue(driver.busy)
        self.assertEqual(driver.turn_id, "t1")

    async def test_busy_send_steers(self):
        peer = FakePeer({"turn/steer": {}})
        driver = make_driver(Events(), peer)
        driver.busy, driver.turn_id = True, "t1"
        await driver.send("also this", images=[{"path": "/tmp/a.png", "mime": "image/png"}])
        steer = peer.sent("turn/steer")[0]
        self.assertEqual(steer["expectedTurnId"], "t1")
        self.assertEqual(steer["input"][0], {"type": "localImage", "path": "/tmp/a.png"})
        self.assertEqual(peer.sent("turn/start"), [])

    async def test_steer_falls_back_to_a_new_turn(self):
        peer = FakePeer({"turn/steer": JsonRpcError(-32000, "no active turn"), "turn/start": {"turn": {"id": "t2"}}})
        driver = make_driver(Events(), peer)
        driver.busy, driver.turn_id = True, "t1"
        await driver.send("late")
        self.assertEqual(len(peer.sent("turn/start")), 1)
        self.assertEqual(driver.turn_id, "t2")

    async def test_full_access_mode(self):
        peer = FakePeer({"turn/start": {"turn": {"id": "t1"}}})
        driver = make_driver(Events(), peer)
        await driver.set_mode("full-access")
        await driver.send("go")
        params = peer.sent("turn/start")[0]
        self.assertEqual((params["approvalPolicy"], params["sandboxPolicy"]), ("never", {"type": "dangerFullAccess"}))
        with self.assertRaises(ValueError):
            await driver.set_mode("bypassPermissions")

    async def test_interrupt_cancels_prompts(self):
        peer = FakePeer({"turn/interrupt": {}})
        events = Events()
        driver = make_driver(events, peer)
        driver.busy, driver.turn_id = True, "t9"
        ask = asyncio.create_task(driver._request("item/commandExecution/requestApproval", {"itemId": "e1", "command": "ls"}))
        await asyncio.sleep(0.01)
        await driver.interrupt()
        self.assertEqual(await ask, {"decision": "cancel"})
        self.assertEqual(peer.sent("turn/interrupt")[0], {"threadId": THREAD_ID, "turnId": "t9"})

    async def test_session_scope_approval(self):
        events = Events()
        events.on_permission = lambda drv, req: drv.answer_permission(req["requestId"], allow=True, scope="session")
        driver = make_driver(events, FakePeer())
        self.assertEqual(await driver._request("item/fileChange/requestApproval", {"itemId": "f1"}), {"decision": "acceptForSession"})

    async def test_user_questions(self):
        events = Events()
        events.on_permission = lambda drv, req: drv.answer_permission(req["requestId"], allow=True, answers={"q1": "B"})
        driver = make_driver(events, FakePeer())
        res = await driver._request("item/tool/requestUserInput", {"itemId": "i1", "questions": [
            {"id": "q1", "header": "Pick", "question": "A or B?", "options": [{"label": "A"}, {"label": "B"}]}]})
        self.assertEqual(res, {"answers": {"q1": {"answers": ["B"]}}})
        self.assertEqual(events.of("permission")[0]["request"]["toolName"], "AskUserQuestion")

    async def test_models(self):
        peer = FakePeer({"model/list": RESPONSES["model/list"][0]})
        models = await make_driver(Events(), peer).models()
        self.assertTrue(any(m["isDefault"] for m in models))
        self.assertTrue(all(m["supportedEffortLevels"] for m in models))


if __name__ == "__main__":
    unittest.main()
