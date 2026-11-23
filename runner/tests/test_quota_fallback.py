"""Tests for Claude Code subscription -> DeepSeek API automatic quota fallback and restore."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from riesz_runner.hub import _model_compatible
from riesz_runner.quota_fallback import (
    AFTER_RESET,
    ClaudeQuotaFallback,
    FALLBACK_MODEL,
    FALLBACK_ROUTE,
    SOURCE_ROUTE,
)
from riesz_runner.session_prefs import SessionPrefs


class _FakeUsage:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data if data is not None else {"windows": [], "plan": "subscription"}
        self.limited: dict | None = None
        self.calls = 0

    def reset_time(self) -> float | None:
        now = time.time()
        candidates: list[float] = []
        if self.limited and self.limited.get("resetsAt") and float(self.limited["resetsAt"]) > now:
            candidates.append(float(self.limited["resetsAt"]))
        for w in self.data.get("windows") or ():
            if float(w.get("percent") or 0) >= 100.0 and w.get("resetsAt"):
                candidates.append(float(w["resetsAt"]))
        return max(candidates) if candidates else None

    async def get(self, force: bool = False) -> dict:
        self.calls += 1
        out = dict(self.data)
        if self.limited and (not self.limited.get("resetsAt") or float(self.limited["resetsAt"]) > time.time()):
            out["limited"] = self.limited
        return out


def _fake_hub(state_dir: Path, *, deepseek_configured: bool = True, harness_sids: set[str] | None = None) -> SimpleNamespace:
    prefs = SessionPrefs(state_dir / "session_settings.json")
    usage = _FakeUsage()
    working: dict[str, bool] = {}
    hosted: dict[str, Any] = {}
    api_calls: list[tuple[str, str, str | None, bool]] = []
    sent: list[tuple[str, str, str | None]] = []
    notices: list[tuple[str, dict]] = []
    auto_resume_calls: list[tuple[str, dict]] = []
    ar_items: dict[str, dict] = {}
    hset = harness_sids or set()

    async def set_api(sid: str, route: str, model: str | None = None, *, _internal: bool = False) -> None:
        api_calls.append((sid, route, model, _internal))
        prefs.remember(sid, api=route)
        if not _internal:
            await hub.quota_fallback.on_manual_api_change(sid, route)

    async def send(sid: str, text: str, attachments: list, message_uuid: str, **kwargs) -> dict:
        sent.append((sid, text, kwargs.get("model")))
        return {"uuid": message_uuid, "held": False}

    async def broadcast_session(sid: str, event: dict) -> None:
        if event.get("ev") == "notice":
            notices.append((sid, event))

    async def ar_on_result(sid: str, result: dict) -> None:
        auto_resume_calls.append((sid, result))

    hub = SimpleNamespace(
        cfg=SimpleNamespace(state_dir=state_dir),
        prefs=prefs,
        usage=usage,
        credentials=SimpleNamespace(configured=lambda p: p == "deepseek" and deepseek_configured),
        harnesses=SimpleNamespace(has=lambda sid: sid in hset),
        store=SimpleNamespace(entries={}),
        hosted=hosted,
        closing=False,
        reload_ready=asyncio.Event(),
        get_hosted=lambda sid: hosted.get(sid),
        session_working=lambda sid: working.get(sid, False),
        session_state=lambda sid: {"alive": True},
        session_summary=lambda sid: {"sessionId": sid},
        broadcast_session=broadcast_session,
        broadcast_global=AsyncMock(),
        set_api=set_api,
        delivery=SimpleNamespace(send=send),
        auto_resume=SimpleNamespace(items=ar_items, on_result=ar_on_result, _changed=AsyncMock()),
        # test helpers
        _working=working,
        _api_calls=api_calls,
        _sent=sent,
        _notices=notices,
        _ar_calls=auto_resume_calls,
    )
    hub.quota_fallback = ClaudeQuotaFallback(hub)
    return hub


class QuotaFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_claude_quota_switches_to_deepseek_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            sid = "s1"
            hub.hosted[sid] = SimpleNamespace(model="claude-opus-4-6", alive=True)
            hub.store.entries[sid] = SimpleNamespace(scanner=SimpleNamespace(meta=SimpleNamespace(model="claude-opus-4-6")))
            resets = time.time() + 3600

            handled = await hub.quota_fallback.on_result(
                sid, {"isError": True, "failure": {"cause": "quota", "message": "weekly limit", "resetsAt": resets}},
            )
            self.assertTrue(handled)
            await asyncio.gather(*list(hub.quota_fallback._tasks))

            self.assertEqual(hub._api_calls, [(sid, FALLBACK_ROUTE, FALLBACK_MODEL, True)])
            self.assertEqual(len(hub._sent), 1)
            self.assertEqual(hub._sent[0][0], sid)
            self.assertIn("DeepSeek API", hub._sent[0][1])
            self.assertEqual(hub._sent[0][2], FALLBACK_MODEL)
            self.assertEqual(hub._ar_calls, [])

            view = hub.quota_fallback.view(sid)
            assert view is not None
            self.assertEqual(view["fromRoute"], SOURCE_ROUTE)
            self.assertEqual(view["originalModel"], "claude-opus-4-6")
            self.assertAlmostEqual(view["resetsAt"], resets, delta=1.0)
            self.assertFalse(view["switching"])

            saved = json.loads((Path(td) / "quota_fallback.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["sessions"]), 1)

    async def test_ignores_non_claude_non_subscription_and_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fail = {"isError": True, "failure": {"cause": "quota", "message": "limit"}}

            # Non-Claude harness ignored
            hub1 = _fake_hub(Path(td) / "h1", harness_sids={"codex-1"})
            self.assertFalse(await hub1.quota_fallback.on_result("codex-1", fail))

            # Missing DeepSeek API key ignored
            hub2 = _fake_hub(Path(td) / "h2", deepseek_configured=False)
            self.assertFalse(await hub2.quota_fallback.on_result("s1", fail))

            # Already on deepseek:key or anthropic:key ignored
            hub3 = _fake_hub(Path(td) / "h3")
            hub3.prefs.remember("s1", api="deepseek:key")
            self.assertFalse(await hub3.quota_fallback.on_result("s1", fail))
            hub3.prefs.remember("s2", api="anthropic:key")
            self.assertFalse(await hub3.quota_fallback.on_result("s2", fail))

            # Non-quota failures (rate_limit / balance) ignored
            self.assertFalse(await hub3.quota_fallback.on_result("s3", {"isError": True, "failure": {"cause": "rate_limit"}}))

    async def test_restores_after_quota_recovery_and_waits_for_busy_turn(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            sid = "s1"
            hub.hosted[sid] = SimpleNamespace(model="claude-sonnet-4-6", alive=True)
            resets = time.time() - 120   # already past reset
            hub.usage.data = {
                "windows": [
                    {"label": "Current session", "kind": "session", "percent": 10.0, "resetsAt": time.time() + 18000},
                    {"label": "Current week (all models)", "kind": "week", "percent": 100.0, "resetsAt": time.time() + 7200},
                ],
                "plan": "subscription",
            }
            # Put session into active fallback state
            hub.prefs.remember(sid, api=FALLBACK_ROUTE)
            hub.quota_fallback.items[sid] = {
                "sessionId": sid, "fromRoute": SOURCE_ROUTE, "toRoute": FALLBACK_ROUTE,
                "originalModel": "claude-sonnet-4-6", "fallbackModel": FALLBACK_MODEL,
                "resetsAt": resets, "switchedAt": time.time() - 600, "resumePending": False, "restorePending": False,
            }

            # 1) `/usage` still reports weekly 100% -> must NOT restore yet; updates resetsAt
            await hub.quota_fallback._tick()
            self.assertIn(sid, hub.quota_fallback.items)
            self.assertEqual(hub._api_calls, [])
            self.assertGreater(hub.quota_fallback.items[sid]["resetsAt"], time.time() + 3600)

            # 2) Quota recovers while session is mid-turn on DeepSeek -> marks restorePending without interrupting!
            hub.usage.data["windows"][1]["percent"] = 0.0
            hub.quota_fallback.items[sid]["resetsAt"] = time.time() - (AFTER_RESET + 5)
            hub._working[sid] = True
            await hub.quota_fallback._tick()
            self.assertIn(sid, hub.quota_fallback.items)
            self.assertTrue(hub.quota_fallback.items[sid]["restorePending"])
            self.assertEqual(hub._api_calls, [])

            # 3) DeepSeek turn ends -> on_result immediately triggers restore back to anthropic:subscription + original model
            hub._working[sid] = False
            handled = await hub.quota_fallback.on_result(sid, {"subtype": "success", "isError": False})
            self.assertFalse(handled)
            await asyncio.gather(*list(hub.quota_fallback._tasks))
            self.assertEqual(hub._api_calls, [(sid, SOURCE_ROUTE, "claude-sonnet-4-6", True)])
            self.assertNotIn(sid, hub.quota_fallback.items)
            self.assertTrue(any("已自动切回" in n[1]["text"] for n in hub._notices))

    async def test_manual_api_change_cancels_auto_restore_and_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            sid = "s1"
            hub.prefs.remember(sid, api=FALLBACK_ROUTE)
            hub.quota_fallback.items[sid] = {
                "sessionId": sid, "fromRoute": SOURCE_ROUTE, "toRoute": FALLBACK_ROUTE,
                "originalModel": "claude-opus-4-6", "fallbackModel": FALLBACK_MODEL,
                "resetsAt": time.time() + 600, "switchedAt": time.time() - 30, "resumePending": False, "restorePending": False,
            }
            hub.quota_fallback._save()

            # Reloaded after restart -> entry preserved
            reloaded = ClaudeQuotaFallback(hub)
            self.assertIn(sid, reloaded.items)
            hub.quota_fallback = reloaded

            # User explicitly keeps deepseek:key -> cancels auto-restore
            await hub.set_api(sid, FALLBACK_ROUTE, _internal=False)
            self.assertNotIn(sid, hub.quota_fallback.items)

    async def test_fallback_failure_delegates_back_to_auto_resume(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            sid = "s1"

            async def broken_set_api(*args, **kwargs):
                raise RuntimeError("bad deepseek key")

            hub.set_api = broken_set_api
            fail = {"cause": "quota", "message": "limit", "resetsAt": time.time() + 3600}
            handled = await hub.quota_fallback.on_result(sid, {"isError": True, "failure": fail})
            self.assertTrue(handled)
            await asyncio.gather(*list(hub.quota_fallback._tasks))
            self.assertNotIn(sid, hub.quota_fallback.items)
            self.assertEqual(len(hub._ar_calls), 1)
            self.assertEqual(hub._ar_calls[0][1]["failure"]["cause"], "quota")

    async def test_model_compatibility_guard(self) -> None:
        self.assertTrue(_model_compatible("anthropic:subscription", "claude-opus-4-6"))
        self.assertFalse(_model_compatible("anthropic:subscription", "deepseek-v4-pro"))
        self.assertFalse(_model_compatible("anthropic:subscription", "<synthetic>"))
        self.assertTrue(_model_compatible("deepseek:key", "deepseek-v4-pro"))
        self.assertFalse(_model_compatible("deepseek:key", "claude-opus-4-6"))


if __name__ == "__main__":
    unittest.main()
