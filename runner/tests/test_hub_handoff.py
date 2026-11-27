"""Hub.handoff bookkeeping: subagent links carried over, held messages after a failed move, and the
reload wait surviving a failing idle check."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from riesz_runner.hub import Hub
from riesz_runner.session_prefs import SessionPrefs

PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
NEW = "33333333-3333-4333-8333-333333333333"

ITEMS = [
    {"kind": "user", "key": "u1", "seq": 1, "text": "整理一下目录", "images": []},
    {"kind": "assistant", "key": "a1", "seq": 2, "blocks": [{"type": "text", "text": "好的，已经整理。"}]},
]


class _Harnesses:
    def __init__(self, fail_import: bool = False) -> None:
        self.records: dict[str, dict] = {}
        self.drivers: dict[str, object] = {}
        # Fails after the target session exists: the move is cut off half way.
        self.set_imported = AsyncMock(side_effect=RuntimeError("cannot write history") if fail_import else None)

    def has(self, sid: str) -> bool:
        return sid in self.records

    def harness_of(self, sid: str) -> str | None:
        return (self.records.get(sid) or {}).get("harness")

    async def create(self, target: str, cwd: str, **_: object) -> SimpleNamespace:
        self.records[NEW] = {"harness": target, "cwd": cwd}
        return SimpleNamespace(session_id=NEW)

    async def discard(self, sid: str) -> None:
        self.records.pop(sid, None)

    def is_working(self, sid: str, *, include_crons: bool = True) -> bool | None:
        return None


def _hub(tmp: Path, *, fail_import: bool = False) -> Hub:
    hub = Hub.__new__(Hub)
    hub.cfg = SimpleNamespace(state_dir=tmp, allow_bypass=False)
    hub.prefs = SessionPrefs(tmp / "session_settings.json")
    hub.harnesses = _Harnesses(fail_import)
    hub._handing_off = set()
    hub._handoff_held = {}
    hub.hosted = {}
    hub.store = SimpleNamespace(get=lambda sid: None, entries={})
    hub.agy_subagents = SimpleNamespace(list=lambda *a, **k: [])
    hub.auto_resume = SimpleNamespace(taken_over=AsyncMock())
    hub.quota_fallback = SimpleNamespace(on_manual_api_change=AsyncMock())
    hub.schedules = SimpleNamespace(reassign=AsyncMock())
    hub.delivery = SimpleNamespace(send=AsyncMock())
    hub.broadcast_global = AsyncMock()
    hub.broadcast_session = AsyncMock()
    hub.stop_process = AsyncMock()
    hub.session_summary = lambda sid: {"sessionId": sid, "cwd": str(tmp), "harness": hub.harnesses.harness_of(sid)}

    async def source_items(sid: str) -> list[dict]:
        return list(ITEMS)

    hub._source_items = source_items
    return hub


class HandoffTest(unittest.IsolatedAsyncioTestCase):
    async def test_subagent_moved_to_another_harness_stays_a_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hub = _hub(Path(d))
            hub.prefs.remember(CHILD, subagentOf=PARENT, startedBy=PARENT, subagentTask="整理目录")

            new_id = await hub.handoff(CHILD, "gemini", mode="default")

            self.assertEqual(new_id, NEW)
            self.assertEqual(hub.prefs.get(CHILD)["handedOffTo"], NEW)
            pref = hub.prefs.get(NEW)
            self.assertEqual(pref["subagentOf"], PARENT)
            self.assertEqual(pref["subagentTask"], "整理目录")
            # The parent lists the session the subagent became, not the one that stepped aside.
            self.assertEqual([c["sessionId"] for c in hub.child_entries(PARENT)], [NEW])

    async def test_messages_held_during_a_failed_handoff_stay_with_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            hub = _hub(Path(d), fail_import=True)
            original = hub._source_items

            async def items_then_message(sid: str) -> list[dict]:
                hub._handoff_held.setdefault(CHILD, []).append(
                    {"uuid": "m1", "text": "还有一件事", "attachments": [], "preview": "还有一件事", "options": {}, "ts": 0})
                return await original(sid)

            hub._source_items = items_then_message
            with self.assertRaises(RuntimeError):
                await hub.handoff(CHILD, "gemini", mode="default")

            hub.delivery.send.assert_awaited_once()
            self.assertEqual(hub.delivery.send.await_args.args[0], CHILD)
            self.assertNotIn(CHILD, hub._handing_off)
            self.assertIsNone((hub.prefs.get(CHILD) or {}).get("handedOffTo"))
            # Aborted handoff discards the half-created orphan target session.
            self.assertNotIn(NEW, hub.harnesses.records)


class ReloadWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_failing_idle_check_does_not_end_the_wait(self) -> None:
        hub = Hub.__new__(Hub)
        hub.reload_pending = True
        hub.reload_since = 0.0
        hub.reload_ready = asyncio.Event()
        hub._working_saved = None
        hub._save_working = lambda working: None
        hub.broadcast_global = AsyncMock()
        hub._reload_waiting_for = lambda: []
        calls = {"n": 0}

        def all_idle() -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transcript unreadable")
            return True

        hub._all_idle = all_idle
        await asyncio.wait_for(hub._reload_when_idle(), timeout=10)
        self.assertTrue(hub.reload_ready.is_set())


if __name__ == "__main__":
    unittest.main()
