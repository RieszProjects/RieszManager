"""Tests for automatic resuming after quota, billing, network or crash failures (auto_resume.py)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from riesz_runner.auto_resume import (
    AFTER_RESET,
    AutoResume,
    BACKOFF,
    EXIT_ATTEMPTS,
    claude_failure,
    codex_failure,
    dsh_failure,
    failure,
    failure_from_text,
)


class ClassifierTest(unittest.TestCase):
    def test_claude_quota_with_reset_time(self) -> None:
        reset = time.time() + 3600
        f = claude_failure(
            {"is_error": True, "result": "You hit your limit", "api_error_status": 429},
            {"error": "rate_limit", "api_error_params": {"rate_limit_info": {"status": "rejected", "resetsAt": reset}}},
            None,
        )
        assert f is not None
        self.assertEqual(f["cause"], "quota")
        self.assertEqual(f["resetsAt"], reset)

    def test_claude_billing_and_non_retryable(self) -> None:
        f = claude_failure({"is_error": True, "result": "billing issue"}, {"error": "billing_error"}, None)
        assert f is not None
        self.assertEqual(f["cause"], "balance")
        self.assertIsNone(claude_failure({"is_error": False, "result": "ok"}, None, None))
        self.assertIsNone(claude_failure({"is_error": True, "result": "invalid request"}, {"error": "invalid_request"}, None))

    def test_codex_quota_and_errors(self) -> None:
        reset = time.time() + 1800
        f = codex_failure(
            {"codexErrorInfo": "usageLimitExceeded", "message": "Usage limit reached"},
            {"primary": {"usedPercent": 100, "resetsAt": reset}, "secondary": {"usedPercent": 40, "resetsAt": reset + 9999}},
        )
        assert f is not None
        self.assertEqual(f["cause"], "quota")
        self.assertEqual(f["resetsAt"], reset)
        self.assertEqual(codex_failure({"codexErrorInfo": "responseStreamDisconnected", "message": "lost"}, None)["cause"], "network")
        self.assertIsNone(codex_failure({"codexErrorInfo": "unauthorized", "message": "bad token"}, None))

    def test_dsh_errors(self) -> None:
        f = dsh_failure({"kind": "error", "error": {"code": "QUOTA", "message": "insufficient balance"}})
        assert f is not None
        self.assertEqual(f["cause"], "balance")
        f2 = dsh_failure({"kind": "error", "error": {"code": "RATE_LIMIT", "message": "slow down", "providerRetryAfterMs": 30_000}})
        assert f2 is not None
        self.assertEqual(f2["cause"], "rate_limit")
        self.assertGreater(f2["resetsAt"], time.time() + 20)
        self.assertIsNone(dsh_failure({"kind": "aborted"}))

    def test_acp_text_classifier(self) -> None:
        self.assertEqual(failure_from_text("HTTP 402 Payment Required")["cause"], "balance")
        self.assertEqual(failure_from_text("RESOURCE_EXHAUSTED: quota exceeded")["cause"], "quota")
        self.assertEqual(failure_from_text("503 Service Unavailable")["cause"], "server")
        self.assertEqual(failure_from_text("ECONNRESET socket hang up")["cause"], "network")
        self.assertIsNone(failure_from_text("Invalid argument: foo"))


def _fake_hub(state_dir: Path) -> SimpleNamespace:
    hub = SimpleNamespace(
        cfg=SimpleNamespace(state_dir=state_dir),
        prefs=SimpleNamespace(get=lambda sid: {}),
        harnesses=SimpleNamespace(records={}, has=lambda sid: False),
        store=SimpleNamespace(entries={"s1": object()}),
        hosted={},
        limits=SimpleNamespace(reset_time=lambda route: None),
        usage=SimpleNamespace(reset_time=lambda: None),
        closing=False,
        reload_ready=asyncio.Event(),
        session_working=lambda sid: False,
        message_queued=lambda sid, u: False,
        session_state=lambda sid: {"alive": False},
        session_summary=lambda sid: {"sessionId": sid},
        broadcast_session=AsyncMock(),
        broadcast_global=AsyncMock(),
        delivery=SimpleNamespace(send=AsyncMock()),
    )
    return hub


class AutoResumeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_quota_schedules_after_reset_and_fires(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            ar = AutoResume(hub)
            reset = time.time() + 600
            await ar.on_result("s1", {"isError": True, "failure": failure("quota", "hit limit", reset)})
            view = ar.view("s1")
            assert view is not None
            self.assertEqual(view["cause"], "quota")
            self.assertAlmostEqual(view["nextAt"], reset + AFTER_RESET, delta=2.0)

            # Persisted across restart
            ar2 = AutoResume(hub)
            self.assertIn("s1", ar2.items)

            # Move deadline to the past and tick: sends resume message
            ar2.items["s1"]["nextAt"] = time.time() - 1
            await ar2._tick()
            hub.delivery.send.assert_awaited_once()
            args = hub.delivery.send.await_args.args
            self.assertEqual(args[0], "s1")
            self.assertIn("自动续跑", args[1])

            # Turn succeeds -> cleared
            await ar2.on_result("s1", {"subtype": "success", "isError": False})
            self.assertIsNone(ar2.view("s1"))

    async def test_backoff_and_user_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            ar = AutoResume(hub)
            t0 = time.time()
            await ar.on_result("s1", {"isError": True, "failure": failure("balance", "402")})
            self.assertAlmostEqual(ar.view("s1")["nextAt"], t0 + BACKOFF[0], delta=2.0)

            # Another attempt fails -> grows backoff
            ar.items["s1"]["attempts"] = 1
            await ar.on_result("s1", {"isError": True, "failure": failure("balance", "402")})
            self.assertAlmostEqual(ar.view("s1")["nextAt"], time.time() + BACKOFF[1], delta=2.0)

            # User sends a message / cancels -> cleared
            self.assertTrue(await ar.taken_over("s1"))
            self.assertIsNone(ar.view("s1"))

    async def test_exit_mid_turn_capped_and_subagent_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            ar = AutoResume(hub)
            await ar.on_exit("s1", during_turn=True, requested=True)
            self.assertIsNone(ar.view("s1"))
            await ar.on_exit("s1", during_turn=False, requested=False)
            self.assertIsNone(ar.view("s1"))
            await ar.on_exit("s1", during_turn=True, requested=False)
            self.assertEqual(ar.view("s1")["cause"], "exit")

            ar.items["s1"]["attempts"] = EXIT_ATTEMPTS
            await ar.on_exit("s1", during_turn=True, requested=False)
            self.assertIsNone(ar.view("s1"))

            # Subagent ignored
            hub.prefs = SimpleNamespace(get=lambda sid: {"subagentOf": "parent"})
            await ar.on_result("sub1", {"isError": True, "failure": failure("quota", "limit")})
            self.assertIsNone(ar.view("sub1"))

    async def test_other_message_succeeding_accelerates_resume(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            ar = AutoResume(hub)
            await ar.on_result("s1", {"isError": True, "failure": failure("balance", "402")})
            # A scheduled message went through before nextAt
            await ar.on_result("s1", {"subtype": "success", "isError": False})
            self.assertLessEqual(ar.view("s1")["nextAt"], time.time() + 1)

    async def test_reconciles_stuck_sent_entry_on_restart_and_idle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            path = Path(td) / "auto_resume.json"
            now = time.time()
            # Persisted in-flight entry across runner restart -> reconciled on load
            path.write_text(json.dumps({"sessions": [{
                "sessionId": "s1", "cause": "network", "message": "timeout",
                "since": now - 300, "attempts": 1, "sentAt": now - 10, "nextAt": None,
            }]}), encoding="utf-8")
            ar = AutoResume(hub)
            view = ar.view("s1")
            assert view is not None
            self.assertIsNone(view["sentAt"])
            self.assertIsNotNone(view["nextAt"])

            # Runtime stuck sentAt while idle and not queued -> reconciled in _tick
            ar.items["s1"]["sentAt"] = now - 120
            ar.items["s1"]["sentUuid"] = "u-1"
            ar.items["s1"]["nextAt"] = None
            hub.message_queued = lambda sid, u: True
            await ar._tick()
            self.assertIsNotNone(ar.items["s1"]["sentAt"])    # still queued/held -> do not reschedule

            hub.message_queued = lambda sid, u: False
            await ar._tick()
            self.assertIsNone(ar.items["s1"]["sentAt"])
            self.assertIsNotNone(ar.items["s1"]["nextAt"])


class AutoResumeGiveUpTest(unittest.IsolatedAsyncioTestCase):
    async def test_resume_that_cannot_be_sent_is_dropped_on_permanent_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            hub.delivery.send = AsyncMock(side_effect=RuntimeError("unknown session"))
            ar = AutoResume(hub)
            await ar.on_result("s1", {"isError": True, "failure": failure("server", "overloaded")})
            ar.items["s1"]["nextAt"] = time.time() - 1
            await ar._tick()
            self.assertNotIn("s1", ar.items)

    async def test_resume_that_keeps_failing_gives_up_after_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            hub.delivery.send = AsyncMock(side_effect=RuntimeError("runner busy"))
            ar = AutoResume(hub)
            await ar.on_result("s1", {"isError": True, "failure": failure("exit", "died")})
            for _ in range(5):
                if "s1" not in ar.items:
                    break
                ar.items["s1"]["nextAt"] = time.time() - 1
                await ar._tick()
            self.assertNotIn("s1", ar.items)     # exit resumes are capped at EXIT_ATTEMPTS, sent or not

    async def test_claude_quota_without_reset_uses_usage_reset_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            reset = time.time() + 1800
            hub.usage = SimpleNamespace(reset_time=lambda: reset)
            ar = AutoResume(hub)
            await ar.on_result("s1", {"isError": True, "failure": failure("quota", "weekly limit")})
            self.assertAlmostEqual(ar.items["s1"]["nextAt"], reset + 60, delta=1)


if __name__ == "__main__":
    unittest.main()
