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
                           reload_ready=asyncio.Event(), get_hosted=lambda sid: None, broadcast_global=broadcast_global)


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


if __name__ == "__main__":
    unittest.main()
