"""Tests for runner timeout handling and /api/config resilience."""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import AsyncRequestFactory, override_settings

from bridge import runner, views


class RunnerConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_request_converts_timeout_to_runner_unavailable(self) -> None:
        fake_session = MagicMock()
        fake_session.request.side_effect = asyncio.TimeoutError("timed out")
        with patch.object(runner, "session", return_value=fake_session):
            with self.assertRaises(runner.RunnerUnavailable):
                await runner.request("GET", "/v1/health", timeout=5)

    @override_settings(OPENAI_API_KEY="sk-test", TRANSCRIBE_LIVE_MODEL="")
    async def test_app_config_returns_200_with_transcription_when_runner_times_out(self) -> None:
        fake_session = MagicMock()
        fake_session.request.side_effect = asyncio.TimeoutError("timed out")
        req = AsyncRequestFactory().get("/api/config")
        req.user = SimpleNamespace(is_authenticated=True, username="dev")
        req.auser = AsyncMock(return_value=req.user)
        req.session = {"_seen": time.time()}
        with patch.object(runner, "session", return_value=fake_session):
            resp = await views.app_config(req)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content)
        self.assertFalse(body["runner"])
        self.assertTrue(body["transcription"])
        self.assertFalse(body["transcriptionLive"])

    async def test_app_config_survives_a_bad_health_body(self) -> None:
        class _Resp:
            status = 200

            def __init__(self, error: Exception | None, value: object = None) -> None:
                self.error, self.value = error, value

            async def json(self):
                if self.error:
                    raise self.error
                return self.value

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        import aiohttp
        cases = [_Resp(asyncio.TimeoutError()), _Resp(aiohttp.ContentTypeError(MagicMock(), ())),
                 _Resp(ValueError("not json")), _Resp(None, ["not", "an", "object"])]
        for fake in cases:
            req = AsyncRequestFactory().get("/api/config")
            req.user = SimpleNamespace(is_authenticated=True, username="dev")
            req.auser = AsyncMock(return_value=req.user)
            req.session = {"_seen": time.time()}
            with patch.object(runner, "request", new=AsyncMock(return_value=fake)):
                resp = await views.app_config(req)
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(json.loads(resp.content)["runner"])

    async def test_streamed_requests_limit_silence_not_the_whole_transfer(self) -> None:
        fake_session = MagicMock()
        fake_session.request = AsyncMock(return_value="resp")
        with patch.object(runner, "session", return_value=fake_session):
            await runner.request("GET", "/v1/files/raw", timeout=600, stream=True)
        limit = fake_session.request.await_args.kwargs["timeout"]
        self.assertIsNone(limit.total)
        self.assertEqual(limit.sock_read, 600)

