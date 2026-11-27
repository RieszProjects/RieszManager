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


if __name__ == "__main__":
    unittest.main()
