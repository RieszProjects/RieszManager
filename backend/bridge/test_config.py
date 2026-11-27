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
