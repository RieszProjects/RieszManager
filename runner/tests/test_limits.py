"""Other subscriptions' usage limits for the doll (limits.py)."""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from typing import Any

from riesz_runner.limits import Limits, codex_windows

SNAPSHOT = {"primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1795220712},
            "secondary": {"usedPercent": 96, "windowDurationMins": 10080, "resetsAt": 1795572611},
            "planType": "plus", "rateLimitReachedType": None}


class FakeCredentials:
    def __init__(self, login: dict[str, Any] | None) -> None:
        self.login = login

    async def subscription(self, provider: str, bins: dict[str, Any]) -> dict[str, Any] | None:
        return self.login if provider == "openai" else None


def hub(login: dict[str, Any] | None) -> Any:
    return SimpleNamespace(credentials=FakeCredentials(login), bins=lambda: {},
                           harnesses=SimpleNamespace(bin=lambda _h: None), cfg=SimpleNamespace(state_dir="/tmp"))


class LimitsTest(unittest.IsolatedAsyncioTestCase):
    def test_codex_windows_like_claude_usage(self):
        windows, limited = codex_windows(SNAPSHOT)
        self.assertEqual([(w["kind"], w["percent"], w["resetsAt"]) for w in windows],
                         [("session", 12.0, 1795220712), ("week", 96.0, 1795572611)])
        self.assertFalse(limited)
        self.assertTrue(codex_windows({**SNAPSHOT, "rateLimitReachedType": "rateLimitReached"})[1])
        self.assertEqual(codex_windows(None), ([], False))

    async def test_a_running_sessions_update_is_used(self):
        limits = Limits(hub({"plan": None}))
        limits.codex_update(SNAPSHOT)
        [entry] = (await limits.get())["limits"]
        self.assertEqual(entry["id"], "openai:subscription")
        self.assertEqual(entry["plan"], "plus")
        self.assertEqual(len(entry["windows"]), 2)
        self.assertIsNone(entry["error"])

    async def test_without_the_login_nothing_is_listed(self):
        self.assertEqual((await Limits(hub(None)).get())["limits"], [])

    async def test_a_failed_check_is_reported(self):
        limits = Limits(hub({"plan": None}))
        limits._live = (time.time() - 3600, SNAPSHOT)              # too old: read again, and codex is missing
        [entry] = (await limits.get())["limits"]
        self.assertIn("codex", entry["error"])


if __name__ == "__main__":
    unittest.main()
