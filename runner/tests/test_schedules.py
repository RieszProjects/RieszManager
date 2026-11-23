"""Schedules keep running while a reload waits for busy sessions (schedules.py)."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from riesz_runner.schedules import Scheduler


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


if __name__ == "__main__":
    unittest.main()
