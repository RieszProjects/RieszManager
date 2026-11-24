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
        self.assertEqual(FALLBACK_MODEL, "deepseek-flash")
        self.assertTrue(_model_compatible("anthropic:subscription", "claude-opus-4-6"))
        self.assertFalse(_model_compatible("anthropic:subscription", "deepseek-flash"))
        self.assertFalse(_model_compatible("anthropic:subscription", "<synthetic>"))
        self.assertTrue(_model_compatible("deepseek:key", "deepseek-flash"))
        self.assertFalse(_model_compatible("deepseek:key", "claude-opus-4-6"))

    async def test_handoff_compact_switches_to_deepseek_on_quota_or_falls_back_cleanly(self) -> None:
        from riesz_runner.hub import Hub

        with tempfile.TemporaryDirectory() as td:
            hub = _fake_hub(Path(td))
            sid = "s1"
            hub._handing_off = {sid}

            # While handing off, quota_fallback.on_result must NOT hijack the session.
            self.assertFalse(await hub.quota_fallback.on_result(
                sid, {"isError": True, "failure": {"cause": "quota", "message": "weekly limit"}},
            ))

            # Bind real Hub._compact_claude onto our fake hub to test quota -> DeepSeek retry!
            compact_items = {"count": 0}

            async def source_items(_sid: str):
                return [{"kind": "compact"}] * compact_items["count"]

            proc = SimpleNamespace(alive=True, working=False, last_result=None)
            hub.hosted[sid] = proc
            hub._source_items = source_items

            calls = {"n": 0}

            async def fake_send(target_sid, text, attachments, uuid, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    proc.last_result = {"isError": True, "ts": time.time(), "failure": {"cause": "quota", "message": "limit"}}
                else:
                    compact_items["count"] = 1
                    proc.last_result = {"isError": False, "ts": time.time(), "subtype": "success"}
                return {"uuid": uuid, "held": False}

            hub.delivery.send = fake_send
            ok = await Hub._compact_claude(hub, sid, timeout=10.0)
            self.assertTrue(ok)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(hub._api_calls, [(sid, FALLBACK_ROUTE, FALLBACK_MODEL, True)])

            # Without DeepSeek key, _compact_claude fails fast (returns False instead of hanging 300s).
            hub2 = _fake_hub(Path(td) / "nokey", deepseek_configured=False)
            proc2 = SimpleNamespace(alive=True, working=False, last_result=None)
            hub2.hosted[sid] = proc2
            hub2._source_items = AsyncMock(return_value=[])

            async def quota_only(target_sid, text, attachments, uuid, **kwargs):
                proc2.last_result = {"isError": True, "ts": time.time(), "failure": {"cause": "quota", "message": "limit"}}
                return {"uuid": uuid, "held": False}

            hub2.delivery.send = quota_only
            t0 = time.monotonic()
            ok2 = await Hub._compact_claude(hub2, sid, timeout=60.0)
            self.assertFalse(ok2)
            self.assertLess(time.monotonic() - t0, 5.0)

    async def test_compact_quota_failure_with_is_error_false_switches_to_deepseek(self) -> None:
        """Real Claude Code emits `result.is_error=false` + `system/status` `compact_result=failed`
        when `/compact` hits a weekly limit; both `ClaudeProcess` and `_compact_claude` must detect it."""
        from riesz_runner.auto_resume import claude_failure
        from riesz_runner.config import Config
        from riesz_runner.hub import Hub
        from riesz_runner.process import ClaudeProcess

        # 1. claude_failure recognizes quota on is_error=False + compact_error (with and without rejected event)
        f1 = claude_failure(
            {"type": "result", "subtype": "success", "is_error": False},
            None,
            {"status": "rejected", "resetsAt": 1795687200},
            "Error during compaction: You've hit your weekly limit · resets Nov 26, 5am",
        )
        self.assertIsNotNone(f1)
        assert f1 is not None
        self.assertEqual(f1["cause"], "quota")
        self.assertEqual(f1["resetsAt"], 1795687200.0)

        f2 = claude_failure(
            {"type": "result", "subtype": "success", "is_error": False},
            None,
            None,
            "Error during compaction: You've hit your weekly limit · resets Nov 26, 5am",
        )
        self.assertIsNotNone(f2)
        assert f2 is not None
        self.assertEqual(f2["cause"], "quota")

        # 2. ClaudeProcess._handle captures compact_error from system/status and populates last_result
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(claude_bin="claude", socket_path=Path(td) / "runner.sock", token="tok",
                         state_dir=Path(td), claude_home=Path(td) / "claude", workspace_root=Path(td))
            events: list[dict[str, Any]] = []

            async def sink(_proc, ev):
                events.append(ev)

            p = ClaudeProcess(cfg, session_id="sess-real", cwd=td, sink=sink)
            await p._handle({"type": "system", "subtype": "status", "status": "compacting"})
            await p._handle({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1795687200}})
            await p._handle({
                "type": "system", "subtype": "status", "status": None,
                "compact_result": "failed",
                "compact_error": "Error during compaction: You've hit your weekly limit · resets Nov 26, 5am",
            })
            await p._handle({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0})
            self.assertIsNotNone(p.last_result)
            assert p.last_result is not None
            self.assertFalse(p.last_result["isError"])
            self.assertEqual((p.last_result.get("failure") or {}).get("cause"), "quota")
            self.assertIn("weekly limit", p.last_result.get("compactError") or "")

            # 3. _compact_claude detects that exact isError=False + failure.cause=quota result and retries on DeepSeek
            hub = _fake_hub(Path(td) / "hub", deepseek_configured=True)
            sid = "s-compact-quota"
            proc = SimpleNamespace(alive=True, working=False, last_result=None)
            hub.hosted[sid] = proc
            compact_items = {"count": 0}
            hub._source_items = AsyncMock(
                side_effect=lambda _sid: [{"kind": "compact"}] * compact_items["count"]
            )
            calls = {"n": 0}

            async def fake_send(target_sid, text, attachments, uuid, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    proc.last_result = {
                        "isError": False,
                        "subtype": "success",
                        "ts": time.time(),
                        "compactError": "Error during compaction: You've hit your weekly limit",
                        "failure": {"cause": "quota", "resetsAt": 1795687200.0, "message": "weekly limit"},
                    }
                else:
                    compact_items["count"] = 1
                    proc.last_result = {"isError": False, "subtype": "success", "ts": time.time()}
                return {"uuid": uuid, "held": False}

            hub.delivery.send = fake_send
            ok = await Hub._compact_claude(hub, sid, timeout=10.0)
            self.assertTrue(ok)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(hub._api_calls, [(sid, FALLBACK_ROUTE, FALLBACK_MODEL, True)])


if __name__ == "__main__":
    unittest.main()
