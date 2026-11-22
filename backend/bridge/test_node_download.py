"""Tests for secret download-token gating on /api/nodes/bootstrap.py and /api/nodes/client.zip."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("RIESZ_SECRET_KEY", "x" * 40)
os.environ.setdefault("RIESZ_PASSWORD", "test-password")
os.environ.setdefault("RUNNER_TOKEN", "r" * 32)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "riesz.settings")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django
django.setup()

from django.conf import settings
from django.test import RequestFactory

from bridge import views


class _FakeResp:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class NodeDownloadGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orig_data_dir = settings.DATA_DIR
        self.orig_dl_tok = settings.NODE_DOWNLOAD_TOKEN
        self.orig_pub = settings.PUBLIC_ORIGIN
        settings.DATA_DIR = Path(self.tmp.name)
        settings.NODE_DOWNLOAD_TOKEN = ""
        settings.PUBLIC_ORIGIN = "https://official.example.test"
        views._cached_download_token = None
        self.rf = RequestFactory()

    def tearDown(self):
        settings.DATA_DIR = self.orig_data_dir
        settings.NODE_DOWNLOAD_TOKEN = self.orig_dl_tok
        settings.PUBLIC_ORIGIN = self.orig_pub
        views._cached_download_token = None

    async def test_missing_and_wrong_token_return_404(self):
        with mock.patch("bridge.views.runner.request") as mock_req:
            r1 = await views.node_bootstrap_py(self.rf.get("/api/nodes/bootstrap.py"))
            r2 = await views.node_bootstrap_py(self.rf.get("/api/nodes/bootstrap.py?token=wrong"))
            r3 = await views.node_client_zip(self.rf.get("/api/nodes/client.zip"))
            r4 = await views.node_client_zip(self.rf.get("/api/nodes/client.zip", HTTP_X_RIESZ_DOWNLOAD_TOKEN="bad"))
            for r in (r1, r2, r3, r4):
                self.assertEqual(r.status_code, 404)
            mock_req.assert_not_called()

    async def test_valid_token_header_query_public_origin_and_rotate(self):
        tok1, pinned = views._get_node_download_token()
        self.assertFalse(pinned)
        self.assertGreaterEqual(len(tok1), 24)

        captured: list[tuple[str, str, dict | None]] = []

        async def fake_runner_request(method: str, path: str, *, params=None, **kwargs):
            captured.append((method, path, params))
            if path == "/v1/nodes/bootstrap.py":
                return _FakeResp(200, b"# bootstrap", {"Content-Type": "text/x-python"})
            return _FakeResp(200, b"PK\x03\x04", {"Content-Type": "application/zip"})

        with mock.patch("bridge.views.runner.request", side_effect=fake_runner_request):
            # Query param works on bootstrap.py and PUBLIC_ORIGIN overrides forged X-Forwarded-Proto
            req = self.rf.get(
                f"/api/nodes/bootstrap.py?token={tok1}",
                HTTP_X_FORWARDED_PROTO="http",
                HTTP_HOST="localhost",
            )
            r_boot = await views.node_bootstrap_py(req)
            self.assertEqual(r_boot.status_code, 200)
            self.assertEqual(r_boot["Cache-Control"], "no-store")
            self.assertEqual(captured[-1][2], {"origin": "https://official.example.test", "token": tok1})

            # Header works on client.zip
            req_zip = self.rf.get("/api/nodes/client.zip", HTTP_X_RIESZ_DOWNLOAD_TOKEN=tok1)
            r_zip = await views.node_client_zip(req_zip)
            self.assertEqual(r_zip.status_code, 200)
            self.assertEqual(r_zip["Cache-Control"], "no-store")

            # Rotate invalidates old token immediately
            tok2 = views._rotate_node_download_token()
            self.assertNotEqual(tok1, tok2)
            r_old = await views.node_bootstrap_py(self.rf.get(f"/api/nodes/bootstrap.py?token={tok1}"))
            self.assertEqual(r_old.status_code, 404)
            r_new = await views.node_bootstrap_py(self.rf.get(f"/api/nodes/bootstrap.py?token={tok2}"))
            self.assertEqual(r_new.status_code, 200)

    async def test_ws_node_allows_missing_origin_while_browser_ws_still_rejects(self):
        from riesz import asgi

        sent_browser: list[dict] = []
        events = iter([{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}])

        async def _recv():
            return next(events, {"type": "websocket.disconnect", "code": 1000})

        async def _send_browser(msg):
            sent_browser.append(msg)

        # Browser WS without Origin header must still be rejected by AllowedHostsOriginValidator
        await asgi._websocket_router(
            {"type": "websocket", "path": "/ws/events", "headers": [(b"host", b"localhost")]},
            _recv,
            _send_browser,
        )
        self.assertTrue(any(m.get("type") == "websocket.close" for m in sent_browser))

        # Node WS without Origin header must bypass AllowedHostsOriginValidator and reach NodeConsumer
        node_called = {"ok": False}

        async def _fake_node_ws(scope, receive, send):
            node_called["ok"] = True

        with mock.patch.object(asgi, "_node_ws", _fake_node_ws):
            await asgi._websocket_router(
                {"type": "websocket", "path": "/ws/node", "headers": [(b"host", b"localhost")]},
                _recv,
                _send_browser,
            )
        self.assertTrue(node_called["ok"])


if __name__ == "__main__":
    unittest.main()
