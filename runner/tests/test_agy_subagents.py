"""Tests for Antigravity built-in subagent (`invoke_subagent`) discovery and transcript parsing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from riesz_runner.agy_subagents import AgySubagentStore


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


class AgySubagentStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_discover_live_status_items_and_kill(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain = Path(td)
            parent = "11111111-1111-4111-8111-111111111111"
            c1 = "22222222-2222-4222-8222-222222222222"
            c2 = "33333333-3333-4333-8333-333333333333"
            p_log = brain / parent / ".system_generated" / "logs" / "transcript.jsonl"
            c1_log = brain / c1 / ".system_generated" / "logs" / "transcript.jsonl"
            c2_log = brain / c2 / ".system_generated" / "logs" / "transcript.jsonl"

            _write_jsonl(p_log, [
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "tool_calls": [
                        {"name": "invoke_subagent", "args": {"Role": '"UI Researcher"', "TypeName": '"research"', "Prompt": '"Inspect layout"'}},
                        {"name": "invoke_subagent", "args": {"Role": '"Build Fixer"', "TypeName": '"self"', "Prompt": '"Run tests"'}},
                    ],
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": (
                        "Created At: 2026-11-22T11:00:00-05:00\nCompleted At: 2026-11-22T11:00:01-05:00\n"
                        "Created the following subagent:\n"
                        + json.dumps({"conversationId": c1, "logAbsoluteUri": f"file://{c1_log}"})
                    ),
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": (
                        "Created the following subagent:\n"
                        + json.dumps({"conversationId": c2, "logAbsoluteUri": f"file://{c2_log}"})
                    ),
                },
            ])

            # c1: currently running a background command
            _write_jsonl(c1_log, [
                {
                    "step_index": 0,
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-22T16:00:01Z",
                    "content": (
                        f"The following is a <SYSTEM_MESSAGE>\n\n<SYSTEM_MESSAGE>\n"
                        f"[Message] timestamp=2026-11-22T16:00:01Z sender={parent} priority=MESSAGE_PRIORITY_HIGH content=Inspect layout\n</SYSTEM_MESSAGE>"
                    ),
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-22T16:00:02Z",
                    "model": "gemini-4-neon-high",
                    "thinking": "Let me run ls first.",
                    "tool_calls": [
                        {"name": "run_command", "args": {"CommandLine": '"ls -la"', "toolSummary": '"List files"'}},
                        {"name": "read_file", "args": {"Path": '"/work/a.txt"'}},
                    ],
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "status": "RUNNING",
                    "content": "Created At: 2026-11-22T11:00:03-05:00\nTool is running as a background task with task id: 22222222/task-1\n",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": "Created At: 2026-11-22T11:00:04-05:00\nCompleted At: 2026-11-22T11:00:04-05:00\nhello world\n",
                },
                {
                    "step_index": 4,
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "content": "<SYSTEM_MESSAGE>\nReminder: work efficiently.\n</SYSTEM_MESSAGE>",
                },
            ])

            # c2: finished cleanly (PLANNER_RESPONSE with text and no tool_calls)
            _write_jsonl(c2_log, [
                {
                    "step_index": 0,
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "content": f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-22T16:00:01Z sender={parent} priority=HIGH content=Run tests\n</SYSTEM_MESSAGE>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "model": "gemini-4-neon-high",
                    "content": "All tests passed!",
                },
            ])

            store = AgySubagentStore(brain)
            listed = store.list(parent, parent_alive=True)
            self.assertEqual(len(listed), 2)
            by_id = {e["agentId"]: e for e in listed}
            self.assertEqual(by_id[f"agy-{c1}"]["description"], "UI Researcher")
            self.assertEqual(by_id[f"agy-{c1}"]["agentType"], "research")
            self.assertTrue(by_id[f"agy-{c1}"]["running"])
            self.assertEqual(by_id[f"agy-{c2}"]["description"], "Build Fixer")
            self.assertFalse(by_id[f"agy-{c2}"]["running"])
            self.assertEqual(store.running_ids(parent, parent_alive=True), [f"agy-{c1}"])
            # When parent process is not alive, nothing can be running.
            self.assertEqual(store.running_ids(parent, parent_alive=False), [])

            # Inspect parsed display items of c1
            res = await store.items(parent, f"agy-{c1}", parent_alive=True)
            self.assertIsNotNone(res)
            items, _mtime, running = res  # type: ignore[misc]
            self.assertTrue(running)
            self.assertEqual([it["kind"] for it in items], ["user", "assistant"])
            self.assertEqual(items[0]["text"], "Inspect layout")
            blocks = items[1]["blocks"]
            self.assertEqual([b["type"] for b in blocks], ["thinking", "tool", "tool"])
            self.assertEqual(blocks[1]["name"], "Bash")
            self.assertEqual(blocks[1]["input"]["command"], "ls -la")
            self.assertEqual(blocks[2]["name"], "Read")
            self.assertEqual(blocks[2]["input"]["file_path"], "/work/a.txt")
            self.assertEqual(blocks[2]["result"]["text"], "hello world")

            # Background task finishes + final reply -> c1 flips to idle!
            _write_jsonl(c1_log, [
                {
                    "step_index": 5,
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "content": '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-22T16:01:00Z sender=22222222/task-1 priority=HIGH content=Task id "22222222/task-1" finished with result: ok\n</SYSTEM_MESSAGE>',
                },
                {
                    "step_index": 6,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "Layout looks great.",
                },
            ])
            res2 = await store.items(parent, f"agy-{c1}", parent_alive=True)
            items2, _mtime2, running2 = res2  # type: ignore[misc]
            self.assertFalse(running2)
            self.assertEqual(items2[-1]["blocks"][0]["text"], "Layout looks great.")

            # Refuses unknown / cross-session subagent IDs
            self.assertIsNone(await store.items(parent, "agy-99999999-9999-4999-8999-999999999999", parent_alive=True))
            self.assertIsNone(await store.items(parent, "not-agy", parent_alive=True))


if __name__ == "__main__":
    unittest.main()
