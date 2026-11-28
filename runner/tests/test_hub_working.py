"""Tests for Hub working-session snapshots across shutdown and reload."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from riesz_runner.hub import Hub


class _StubHarnesses:
    def __init__(self, ids: list[str] | None = None) -> None:
        self.ids = list(ids or [])
        self.close = AsyncMock()

    def working_ids(self, *, include_crons: bool = True) -> list[str]:
        return list(self.ids)


def _make_stub_hub(tmp: Path, working_ids: list[str]) -> Hub:
    hub = Hub.__new__(Hub)
    hub._working_file = tmp / "working.json"
    hub._working_saved = None
    hub.reload_ready = asyncio.Event()
    hub.closing = False
    hub._tasks = []
    hub.hosted = {}
    hub.harnesses = _StubHarnesses(working_ids)
    hub.browser = SimpleNamespace(stop=AsyncMock())
    hub.deepseek_proxy = SimpleNamespace(close=AsyncMock())
    hub.nodes = SimpleNamespace(_live={}, kick_device=AsyncMock())
    return hub


class HubWorkingSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_flushes_idle_state_so_finished_session_is_not_marked_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            hub = _make_stub_hub(tmp, ["sess-1"])
            # Simulate a 5-second periodic snapshot taken while sess-1 was still busy.
            hub._save_working({"sess-1"})
            self.assertEqual(json.loads(hub._working_file.read_text())["sessions"], ["sess-1"])

            # sess-1 finishes 1 second before SIGTERM / hub.close().
            hub.harnesses.ids = []
            await hub.close()

            self.assertEqual(json.loads(hub._working_file.read_text())["sessions"], [])

    async def test_close_records_newly_started_session_before_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            hub = _make_stub_hub(tmp, [])
            hub._save_working(set())

            # sess-2 starts right before SIGTERM / hub.close().
            hub.harnesses.ids = ["sess-2"]
            await hub.close()

            self.assertEqual(json.loads(hub._working_file.read_text())["sessions"], ["sess-2"])

    async def test_close_during_graceful_reload_keeps_working_empty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            hub = _make_stub_hub(tmp, ["sess-3"])
            hub._save_working(set())
            hub.reload_ready.set()

            await hub.close()

            self.assertEqual(json.loads(hub._working_file.read_text())["sessions"], [])

    def test_mark_interrupted_notifies_idle_parent_of_interrupted_subagent(self) -> None:
        from riesz_runner.session_prefs import SessionPrefs
        from riesz_runner.tools import SubagentWatcher

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            hub = _make_stub_hub(tmp, [])
            hub.cfg = SimpleNamespace(state_dir=tmp)
            hub.prefs = SessionPrefs(tmp / "session_settings.json")
            hub.final_handoff_target = lambda sid: sid
            hub.subagent_watcher = SubagentWatcher(hub)

            hub.prefs.remember("child-1", subagentOf="parent-idle")
            hub.prefs.remember("child-2", subagentOf="parent-busy")
            hub.subagent_watcher._deferred.add("child-1")

            hub._working_file.write_text(
                json.dumps({"sessions": ["child-1", "child-2", "parent-busy"], "time": 1234.0}),
                encoding="utf-8",
            )
            hub._mark_interrupted()

            # Idle parent gets a queued callback telling it child-1 was interrupted; deferred is cleared.
            self.assertIn("child-1", hub.subagent_watcher._pending)
            self.assertEqual(hub.subagent_watcher._pending["child-1"]["parent"], "parent-idle")
            self.assertNotIn("child-1", hub.subagent_watcher._deferred)
            # Parent that was itself interrupted already has its own red dot, so no duplicate callback.
            self.assertNotIn("child-2", hub.subagent_watcher._pending)
            self.assertEqual(hub.prefs.interrupted("parent-busy"), 1234.0)
            self.assertEqual(hub.prefs.interrupted("child-1"), 1234.0)

    async def test_idle_reaper_keeps_parent_alive_while_subagent_is_working(self) -> None:
        from riesz_runner.harnesses import Harnesses, REAP_INTERVAL

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            hub = _make_stub_hub(tmp, [])
            hub.cfg = SimpleNamespace(state_dir=tmp, idle_timeout=60)
            hub.broadcast_global = AsyncMock()
            hub.has_working_subagent = lambda sid: sid == "parent-1"

            harnesses = Harnesses.__new__(Harnesses)
            harnesses.hub = hub
            harnesses.cfg = hub.cfg
            harnesses.records = {"parent-1": {"id": "parent-1", "harness": "codex"}}
            driver = SimpleNamespace(alive=True, ready=True, harness="codex", busy=False, permissions={}, tasks={},
                                     started_at=100.0, has_pending_queue=lambda: False)
            harnesses.drivers = {"parent-1": driver}
            harnesses._locks = {}
            harnesses._active = {"parent-1": 100.0}  # stale (> 60s old)
            harnesses.stop = AsyncMock()

            tick_n = max(1, int(REAP_INTERVAL // 3))
            await harnesses._tick(tick_n, {}, {}, {})

            harnesses.stop.assert_not_awaited()
            self.assertGreater(harnesses._active["parent-1"], 1000.0)

    async def test_subagent_oom_exit_with_partial_text_reports_crash_not_completion(self) -> None:
        from riesz_runner.session_prefs import SessionPrefs
        from riesz_runner.tools import SubagentWatcher

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            hub = _make_stub_hub(tmp, [])
            hub.cfg = SimpleNamespace(state_dir=tmp)
            hub.prefs = SessionPrefs(tmp / "session_settings.json")
            hub.prefs.remember("child-oom", subagentOf="parent-1")
            hub.final_handoff_target = lambda sid: sid
            hub.harnesses = SimpleNamespace(
                has=lambda sid: sid == "child-oom",
                items=lambda sid: [{"kind": "assistant", "key": "a:1", "blocks": [{"type": "text", "text": "开始跑渲染…"}]}],
                summary=lambda sid: {"sessionId": sid},
            )
            hub.delivery = SimpleNamespace(send=AsyncMock())
            hub.broadcast_global = AsyncMock()
            hub.session_summary = lambda sid: {"sessionId": sid}
            watcher = SubagentWatcher(hub)

            await watcher._deliver_exit("child-oom", {"code": -9, "duringTurn": True, "requested": False})

            hub.delivery.send.assert_awaited_once()
            sent_text = hub.delivery.send.await_args.args[1]
            self.assertIn("进程异常退出", sent_text)
            self.assertIn("内存不足", sent_text)
            self.assertIn("subagent_send", sent_text)
            self.assertIn("开始跑渲染…", sent_text)
            self.assertNotIn("这一轮做完了", sent_text)


if __name__ == "__main__":
    unittest.main()
