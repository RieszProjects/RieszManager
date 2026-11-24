"""Schedules keep running while a reload waits for busy sessions (schedules.py)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from riesz_runner.schedules import Scheduler, next_cron


class FakeDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, sid, text, attachments, message_uuid):
        self.sent.append((sid, text))
        return {"uuid": message_uuid, "held": False}


def fake_hub(tmp: Path) -> SimpleNamespace:
    async def broadcast_global(event):
        return None
    return SimpleNamespace(cfg=SimpleNamespace(state_dir=tmp), delivery=FakeDelivery(), reload_pending=False,
                           reload_ready=asyncio.Event(), get_hosted=lambda sid: None,
                           message_queued=lambda sid, u: False, broadcast_global=broadcast_global)


class ReloadTest(unittest.IsolatedAsyncioTestCase):
    async def due(self, hub) -> Scheduler:
        sched = Scheduler(hub)
        s = await sched.create({"sessionId": "6e0f5a52-0000-4000-8000-000000000001", "prompt": "早上好", "every": 60})
        s["next"] = time.time() - 5                     # due now
        return sched

    async def test_a_waiting_reload_does_not_stop_schedules(self):
        hub = fake_hub(Path(tempfile.mkdtemp()))
        sched = await self.due(hub)
        hub.reload_pending = True
        await sched._tick()
        self.assertEqual([t for _, t in hub.delivery.sent], ["早上好"])

    async def test_nothing_fires_while_the_runner_restarts(self):
        hub = fake_hub(Path(tempfile.mkdtemp()))
        sched = await self.due(hub)
        hub.reload_pending = True
        hub.reload_ready.set()
        await sched._tick()
        self.assertEqual(hub.delivery.sent, [])

    async def test_skips_queued_across_any_harness(self):
        hub = fake_hub(Path(tempfile.mkdtemp()))
        sched = await self.due(hub)
        [s] = sched.items.values()
        s["lastUuid"] = "prev-uuid"
        hub.message_queued = lambda sid, u: u == "prev-uuid"
        await sched._tick()
        self.assertEqual(hub.delivery.sent, [])
        self.assertIn("skipped", s["lastResult"])

    async def test_transient_failure_retries_without_advancing_slot(self):
        hub = fake_hub(Path(tempfile.mkdtemp()))
        fail = {"on": True}
        orig_send = hub.delivery.send

        async def flaky(sid, text, attachments, message_uuid):
            if fail["on"]:
                raise RuntimeError("transient error")
            return await orig_send(sid, text, attachments, message_uuid)

        hub.delivery.send = flaky
        sched = await self.due(hub)
        [s] = sched.items.values()
        orig_next_base = s["nextBase"]
        await sched._tick()
        self.assertEqual(s["retries"], 1)
        self.assertIsNone(s["lastBase"])
        self.assertEqual(s["nextBase"], orig_next_base)
        self.assertAlmostEqual(s["next"], time.time() + 60, delta=3.0)

        fail["on"] = False
        s["next"] = time.time() - 1
        await sched._tick()
        self.assertEqual(s["retries"], 0)
        self.assertEqual(s["lastResult"], "sent")
        self.assertEqual(s["lastBase"], orig_next_base)

    async def test_due_schedules_fire_concurrently(self):
        hub = fake_hub(Path(tempfile.mkdtemp()))

        async def slow_send(sid, text, attachments, message_uuid):
            await asyncio.sleep(0.25)
            hub.delivery.sent.append((sid, text))
            return {"uuid": message_uuid, "held": False}

        hub.delivery.send = slow_send
        sched = Scheduler(hub)
        s1 = await sched.create({"sessionId": "6e0f5a52-0000-4000-8000-000000000001", "prompt": "A", "every": 60})
        s2 = await sched.create({"sessionId": "6e0f5a52-0000-4000-8000-000000000002", "prompt": "B", "every": 60})
        s1["next"] = s2["next"] = time.time() - 1
        t0 = time.monotonic()
        await sched._tick()
        self.assertLess(time.monotonic() - t0, 0.45)
        self.assertEqual({t for _, t in hub.delivery.sent}, {"A", "B"})

    async def test_unified_list_all_includes_handoff_agy_and_claude_timers(self):
        import json
        from datetime import datetime, timezone
        from riesz_runner.agy_subagents import AgySubagentStore
        from riesz_runner.config import Config
        from riesz_runner.process import ClaudeProcess

        tmp = Path(tempfile.mkdtemp())
        hub = fake_hub(tmp)
        old_sid = "6e0f5a52-0000-4000-8000-000000000001"
        new_sid = "6e0f5a52-0000-4000-8000-000000000002"
        agy_sid = "6e0f5a52-0000-4000-8000-000000000003"
        hub.final_handoff_target = lambda sid: new_sid if sid == old_sid else sid

        # 1) Antigravity built-in schedule timer in transcript
        agy_store = AgySubagentStore(tmp / "brain")
        tlog = tmp / "brain" / agy_sid / ".system_generated" / "logs" / "transcript.jsonl"
        tlog.parent.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tlog.write_text(
            json.dumps({
                "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": now_iso,
                "tool_calls": [{"name": "schedule", "args": {"DurationSeconds": "600", "Prompt": '"check build"'}}],
            }) + "\n" +
            json.dumps({
                "type": "GENERIC", "status": "RUNNING", "created_at": now_iso,
                "content": f"Tool is running as a background task with task id: {agy_sid}/task-1\nTask Description: Timer: 10m, Prompt: check build\n",
            }) + "\n",
            encoding="utf-8",
        )
        hub.agy_subagents = agy_store
        hub.harnesses = SimpleNamespace(drivers={
            agy_sid: SimpleNamespace(alive=True, harness="antigravity", started_at=time.time() - 60),
        })

        # 2) Claude Code built-in ScheduleWakeup + CronCreate / CronDelete
        cfg = Config(workspace_root=tmp, claude_home=tmp, claude_bin="claude", state_dir=tmp, socket_path=tmp / "s.sock", token="t")
        proc = ClaudeProcess(cfg, session_id=new_sid, cwd=str(tmp), sink=lambda p, e: asyncio.sleep(0))
        proc.alive = True
        self.assertTrue(proc._track_timers("assistant", {
            "message": {"content": [
                {"type": "tool_use", "id": "tu-1", "name": "ScheduleWakeup", "input": {"delaySeconds": 300, "reason": "check tests"}},
                {"type": "tool_use", "id": "tu-2", "name": "CronCreate", "input": {"cron": "*/15 * * * *", "prompt": "poll status", "recurring": True}},
            ]}
        }))
        hub.hosted = {new_sid: proc}

        # 3) Runner schedule originally on old_sid (handed off to new_sid)
        sched = Scheduler(hub)
        await sched.create({"sessionId": old_sid, "prompt": "早上好", "every": 60})

        all_items = sched.list_all()
        self.assertEqual(len(all_items), 4)
        for_new = sched.list_all(new_sid)
        self.assertEqual(len(for_new), 3)
        self.assertEqual(sum(1 for x in for_new if not x["readonly"]), 1)
        self.assertEqual(sum(1 for x in for_new if x["readonly"]), 2)
        for_agy = sched.list_all(agy_sid)
        self.assertEqual(len(for_agy), 1)
        self.assertTrue(for_agy[0]["readonly"])
        self.assertIn("check build", for_agy[0]["prompt"])

        # Reassign updates stored sessionId directly
        moved = await sched.reassign(old_sid, new_sid)
        self.assertEqual(moved, 1)
        self.assertEqual(next(iter(sched.items.values()))["sessionId"], new_sid)

        # CronDelete removes the cron timer
        self.assertTrue(proc._track_timers("assistant", {
            "message": {"content": [{"type": "tool_use", "id": "tu-3", "name": "CronDelete", "input": {"id": "tu-2"}}]}
        }))
        self.assertEqual(len(proc.active_schedules()), 1)

        # Verify next_cron calculation
        nxt = next_cron("*/5 * * * *", 1795470000.0, "UTC")
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, 1795470000.0)

    async def test_scheduler_load_and_fire_follow_handoff_and_prefs_reassign_parent(self) -> None:
        from riesz_runner.session_prefs import SessionPrefs

        old_sid = "11111111-1111-4111-8111-111111111111"
        new_sid = "22222222-2222-4222-8222-222222222222"
        child_sid = "33333333-3333-4333-8333-333333333333"

        with tempfile.TemporaryDirectory() as td:
            # 1. Legacy schedules.json pointing at an old handed-off session auto-migrates on load
            (Path(td) / "schedules.json").write_text(json.dumps({
                "schedules": [{
                    "id": "legacy01",
                    "sessionId": old_sid,
                    "prompt": "ping",
                    "every": 30,
                    "times": [],
                    "days": [],
                    "tz": "UTC",
                    "jitter": 0,
                    "catchUp": False,
                    "enabled": True,
                    "next": time.time() - 5,
                    "nextBase": time.time() - 5,
                    "last": None,
                    "lastBase": None,
                    "lastResult": None,
                    "lastUuid": "prev-u",
                    "retries": 0,
                }],
            }), encoding="utf-8")

            hub = fake_hub(Path(td))
            hub.final_handoff_target = lambda sid: new_sid if sid == old_sid else sid
            queued_checked: list[str] = []
            hub.message_queued = lambda sid, _u: (queued_checked.append(sid), False)[1]

            sched = Scheduler(hub)
            self.assertEqual(sched.items["legacy01"]["sessionId"], new_sid)
            # Even if still pointing at old_sid dynamically, _fire resolves before checking message_queued
            sched.items["legacy01"]["sessionId"] = old_sid
            await sched._fire(sched.items["legacy01"])
            self.assertEqual(queued_checked, [new_sid])
            self.assertEqual(sched.items["legacy01"]["sessionId"], new_sid)
            self.assertEqual(hub.delivery.sent[0][0], new_sid)

            # 2. SessionPrefs.reassign_parent moves subagentOf to new parent
            prefs = SessionPrefs(Path(td) / "prefs.json")
            prefs.remember(child_sid, subagentOf=old_sid, subagentTask="work")
            self.assertEqual(prefs.children(old_sid), [child_sid])
            moved = prefs.reassign_parent(old_sid, new_sid)
            self.assertEqual(moved, 1)
            self.assertEqual(prefs.children(old_sid), [])
            self.assertEqual(prefs.children(new_sid), [child_sid])


if __name__ == "__main__":
    unittest.main()
