"""Tests for Antigravity built-in subagent (`invoke_subagent`) discovery and transcript parsing."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
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

    async def test_parent_open_bg_tasks_and_started_at_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain = Path(td)
            parent = "44444444-4444-4444-8444-444444444444"
            p_log = brain / parent / ".system_generated" / "logs" / "transcript.jsonl"
            store = AgySubagentStore(brain)

            # Old background task from a previous agy process (before started_at)
            _write_jsonl(p_log, [{
                "step_index": 1,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-20T10:00:00Z",
                "content": "Tool is running as a background task with task id: 44444444/task-old\n",
            }])
            from datetime import datetime
            new_start = datetime.fromisoformat("2026-11-23T10:00:00+00:00").timestamp()
            self.assertFalse(store.has_running(parent, parent_alive=True))
            self.assertFalse(store.has_active_work(parent, parent_alive=True, started_at=new_start))

            # New background task started during the current agy process
            _write_jsonl(p_log, [{
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-23T10:05:00Z",
                "content": "Tool is running as a background task with task id: 44444444/task-live\n",
            }])
            self.assertTrue(store.has_active_work(parent, parent_alive=True, started_at=new_start))
            self.assertFalse(store.has_active_work(parent, parent_alive=False, started_at=new_start))

            # Finished / canceled clears it
            _write_jsonl(p_log, [{
                "step_index": 3,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "created_at": "2026-11-23T10:06:00Z",
                "content": 'Task id "44444444/task-live" was canceled by user',
            }])
            self.assertFalse(store.has_active_work(parent, parent_alive=True, started_at=new_start))

            # Killed via manage_task also clears it
            _write_jsonl(p_log, [
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-23T10:07:00Z",
                    "content": "Tool is running as a background task with task id: 44444444/task-kill\n",
                },
            ])
            self.assertTrue(store.has_active_work(parent, parent_alive=True, started_at=new_start))
            _write_jsonl(p_log, [{
                "step_index": 5,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [{"name": "manage_task", "args": {"Action": '"kill"', "TaskId": '"44444444/task-kill"'}}],
            }])
            self.assertFalse(store.has_active_work(parent, parent_alive=True, started_at=new_start))

            # Truncated tool output / file read quoting the marker string must NOT create a phantom task
            _write_jsonl(p_log, [{
                "step_index": 6,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-23T10:08:00Z",
                "content": (
                    "Created At: 2026-11-23T10:08:00-05:00\n"
                    "The tool's output was truncated due to being too long.\n"
                    'Tool is running as a background task with task id: 44444444/task-live\\nTask\n'
                    'BG_TASK_START_RE = re.compile(r"Tool is running as a background task with task id:\\s*([^\\s]+)")\n'
                ),
            }])
            self.assertFalse(store.has_active_work(parent, parent_alive=True, started_at=new_start))

            # One-shot schedule timer clears immediately when it fires via SYSTEM_MESSAGE sender=<tid>
            _write_jsonl(p_log, [
                {
                    "step_index": 7,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-23T10:09:00Z",
                    "tool_calls": [{"name": "schedule", "args": {"DurationSeconds": "90", "Prompt": '"Check lock"'}}],
                },
                {
                    "step_index": 8,
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-23T10:09:01Z",
                    "content": (
                        "Created At: 2026-11-23T10:09:01-05:00\n"
                        "Tool is running as a background task with task id: 44444444/task-timer1\n"
                        "Task Description: Timer: 1m 30s, Prompt: Check lock\n"
                    ),
                },
            ])
            # Timer is in the past relative to wall-clock time unless createdAt is recent, so use a fresh timestamp
            now_iso = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _write_jsonl(p_log, [
                {
                    "step_index": 9,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": now_iso,
                    "tool_calls": [{"name": "schedule", "args": {"DurationSeconds": "180", "Prompt": '"Check progress"'}}],
                },
                {
                    "step_index": 10,
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": now_iso,
                    "content": (
                        f"Created At: {now_iso}\n"
                        "Tool is running as a background task with task id: 44444444/task-timer2\n"
                        "Task Description: Timer: 3m, Prompt: Check progress\n"
                    ),
                },
            ])
            self.assertTrue(store.has_active_work(parent, parent_alive=True, started_at=new_start))
            self.assertEqual(len(store.active_schedules(parent, parent_alive=True, started_at=new_start)), 1)

            # Firing delivers a SYSTEM_MESSAGE with sender=<tid> (no "Task id ... finished" line)
            _write_jsonl(p_log, [{
                "step_index": 11,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "created_at": now_iso,
                "content": (
                    "<SYSTEM_MESSAGE>\n"
                    f"[Message] timestamp={now_iso} sender=44444444/task-timer2 priority=MESSAGE_PRIORITY_HIGH content=Check progress\n"
                    "</SYSTEM_MESSAGE>"
                ),
            }])
            self.assertFalse(store.has_active_work(parent, parent_alive=True, started_at=new_start))
            self.assertEqual(store.active_schedules(parent, parent_alive=True, started_at=new_start), [])


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgyTimerBookkeepingTest(unittest.TestCase):
    PARENT = "44444444-4444-4444-8444-444444444444"

    def test_failed_schedule_call_does_not_shift_later_timers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain = Path(td)
            log = brain / self.PARENT / ".system_generated" / "logs" / "transcript.jsonl"
            now = time.time()
            _write_jsonl(log, [
                # A cron the tool turned down: no "running as a background task" line follows it.
                {"type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(now - 1000),
                 "tool_calls": [{"name": "schedule", "args": {"CronExpression": '"*/61 * * * *"', "Prompt": '"bad"'}}]},
                {"type": "GENERIC", "status": "ERROR", "created_at": _iso(now - 999), "content": "invalid cron expression"},
                {"type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(now - 900),
                 "tool_calls": [{"name": "schedule", "args": {"DurationSeconds": "60", "Prompt": '"ping"'}}]},
                {"type": "GENERIC", "status": "RUNNING", "created_at": _iso(now - 899),
                 "content": "Tool is running as a background task with task id: 44444444/task-9\n"
                            "Task Description: Timer: 1m, Prompt: ping in 1m\n"},
            ])
            store = AgySubagentStore(brain)
            sched = store.active_schedules(self.PARENT, parent_alive=True, started_at=now - 3600)
            self.assertEqual(len(sched), 0)                       # fired long ago (60 s timer, 15 min back)
            info = store._parents[self.PARENT].open_schedules["44444444/task-9"]
            self.assertIsNone(info["cron"])
            self.assertEqual(info["duration"], 60.0)
            self.assertEqual(info["prompt"], "ping in 1m")
            self.assertFalse(store.has_active_work(self.PARENT, parent_alive=True, started_at=now - 3600))

    def test_timer_description_durations(self) -> None:
        from riesz_runner.agy_subagents import _timer_seconds
        self.assertEqual(_timer_seconds("3m 35s"), 215.0)
        self.assertEqual(_timer_seconds("45s"), 45.0)
        self.assertEqual(_timer_seconds("1h 5m"), 3900.0)
        self.assertIsNone(_timer_seconds("*/5 * * * *"))

    def test_background_command_progress_message_does_not_end_a_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain = Path(td)
            parent, child = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
            p_log = brain / parent / ".system_generated" / "logs" / "transcript.jsonl"
            c_log = brain / child / ".system_generated" / "logs" / "transcript.jsonl"
            _write_jsonl(p_log, [
                {"type": "PLANNER_RESPONSE", "status": "DONE",
                 "tool_calls": [{"name": "invoke_subagent", "args": {"Role": '"R"', "Prompt": '"x"'}}]},
                {"type": "GENERIC", "status": "DONE", "content": "Created the following subagent:\n"
                 + json.dumps({"conversationId": child, "logAbsoluteUri": f"file://{c_log}"})},
            ])
            task = f"{child}/task-5"
            _write_jsonl(c_log, [
                {"type": "PLANNER_RESPONSE", "status": "DONE",
                 "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"sleep 900"'}}]},
                {"type": "GENERIC", "status": "RUNNING",
                 "content": f"Tool is running as a background task with task id: {task}\nTask Description: sleep 900\n"},
                {"type": "SYSTEM_MESSAGE", "status": "DONE", "content":
                    f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T04:22:38Z sender={task} priority=MESSAGE_PRIORITY_HIGH "
                    "content=Background command is still running after 180s: sleep 900\n</SYSTEM_MESSAGE>"},
                {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "Still waiting."},
            ])
            store = AgySubagentStore(brain)
            self.assertTrue(store.list(parent, parent_alive=True)[0]["running"])
            self.assertTrue(store.has_active_work(parent, parent_alive=True))


if __name__ == "__main__":
    unittest.main()
