"""Tests for backend HTML preview serving, CORS, Range, and local URL rewriting."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote, unquote

os.environ.setdefault("RIESZ_SECRET_KEY", "x" * 40)
os.environ.setdefault("RIESZ_PASSWORD", "test-password")
os.environ.setdefault("RUNNER_TOKEN", "r" * 32)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "riesz.settings")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django
django.setup()

from django.core import signing
from django.test import RequestFactory

from bridge import viewer


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]


class _FakeResp:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = _FakeContent(body)

    async def read(self) -> bytes:
        return self._body

    def release(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ViewerRewriteTest(unittest.TestCase):
    def setUp(self):
        self.tok = viewer.make_token("demo-app")
        self.base = f"/api/view/{quote(self.tok, safe='')}/"
        self.resolve = viewer._build_resolver(
            base_url=self.base,
            project_root="/workspace/demo-app",
            file_path="/workspace/demo-app/dist/index.html",
            workspace_root="/workspace",
            view_prefix="",
        )

    def test_relative_and_external_urls_preserved(self):
        for u in (
            "img/card.webp",
            "./assets/design.css",
            "../mods/core/a.webp",
            "https://example.com/a.png",
            "http://example.com/a.js",
            "//cdn.example.com/a.js",
            "data:image/png;base64,AAA",
            "blob:https://example.com/123",
            "#section-1",
            "mailto:a@b.com",
        ):
            self.assertEqual(self.resolve(u), u)

    def test_file_and_absolute_urls_rewritten(self):
        # Same project via file:// and absolute path
        self.assertEqual(
            self.resolve("file:///workspace/demo-app/design/img/中文 图.webp?v=1"),
            f"{self.base}design/img/%E4%B8%AD%E6%96%87%20%E5%9B%BE.webp?v=1",
        )
        self.assertEqual(
            self.resolve("/workspace/demo-app/assets/design.css"),
            f"{self.base}assets/design.css",
        )

        # Cross-project workspace file -> single-file workspace token
        other = self.resolve("file:///workspace/other-app/img/shot.png")
        self.assertTrue(other.startswith("/api/view/"))
        self.assertTrue(other.endswith("/shot.png"))
        tok_part = unquote(other.split("/")[3])
        self.assertEqual(signing.loads(tok_part, salt=viewer.SALT), {"p": "other-app", "f": "img/shot.png"})

        # Cross-project via ../../
        up = self.resolve("../../other-app/img/shot.png")
        tok_up = unquote(up.split("/")[3])
        self.assertEqual(signing.loads(tok_up, salt=viewer.SALT), {"p": "other-app", "f": "img/shot.png"})

        # /tmp file -> single-file abs token
        tmp_u = self.resolve("file:///tmp/chart.png")
        tok_tmp = unquote(tmp_u.split("/")[3])
        self.assertEqual(signing.loads(tok_tmp, salt=viewer.SALT), {"abs": "/tmp/chart.png"})

        # Sensitive paths are never turned into tokens
        self.assertEqual(self.resolve("file:///etc/passwd"), "file:///etc/passwd")
        self.assertEqual(self.resolve("file:///workspace/demo-app/.env"), "file:///workspace/demo-app/.env")

    def test_root_relative_includes_from_dir(self):
        self.assertEqual(
            self.resolve("/assets/app.js?v=2"),
            f"{self.base}assets/app.js?v=2&__rz_from=dist",
        )

    def test_shared_view_prefix_preserved(self):
        resolve = viewer._build_resolver(
            base_url=self.base,
            project_root="/tmp/demo",
            file_path="/tmp/demo/index.html",
            workspace_root="/workspace",
            view_prefix="abcdef0123456789",
        )
        self.assertEqual(resolve("/style.css"), f"{self.base}abcdef0123456789/style.css")
        self.assertEqual(resolve("file:///tmp/demo/img/a.png"), f"{self.base}abcdef0123456789/img/a.png")

    def test_rewrite_html_skips_script_body_and_comments(self):
        doc = (
            '<!-- <img src="/ignored.png"> -->'
            '<link rel="stylesheet" href="/css/main.css">'
            '<style>/* url("/c.png") */ @font-face{src:url("/fonts/a.ttf");}</style>'
            '<script type="module" src="/js/app.js">const s = "/keep/me";</script>'
            '<img src="file:///workspace/demo-app/a.webp" srcset="rel.webp 1x, /hi.webp 2x" style="background:url(/bg.png)">'
        )
        out = viewer.rewrite_html(doc, self.resolve)
        self.assertIn('<!-- <img src="/ignored.png"> -->', out)
        self.assertIn(f'href="{self.base}css/main.css?__rz_from=dist"', out)
        self.assertIn('/* url("/c.png") */', out)
        self.assertIn(f'url("{self.base}fonts/a.ttf?__rz_from=dist")', out)
        self.assertIn(f'<script type="module" src="{self.base}js/app.js?__rz_from=dist">const s = "/keep/me";</script>', out)
        self.assertIn(f'src="{self.base}a.webp"', out)
        self.assertIn(f'srcset="rel.webp 1x, {self.base}hi.webp?__rz_from=dist 2x"', out)
        self.assertIn(f'background:url(&quot;{self.base}bg.png?__rz_from=dist&quot;)', out)


class ViewerServeAsyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.tok = viewer.make_token("demo-app")

    async def test_options_preflight_returns_cors(self):
        req = self.rf.options(f"/api/view/{self.tok}/fonts/a.ttf")
        resp = await viewer.serve(req, self.tok, "fonts/a.ttf")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")
        self.assertIn("Range", resp["Access-Control-Allow-Headers"])

    async def test_html_serve_rewrites_and_adds_cors(self):
        body = b'<img src="/img/a.webp"><script type="module" src="app.js"></script>'
        fake = _FakeResp(
            200,
            body,
            headers={
                "Content-Type": "text/html",
                "Content-Length": str(len(body)),
                "X-Riesz-Project-Root": "/workspace/demo-app",
                "X-Riesz-File-Path": "/workspace/demo-app/design/p.html",
                "X-Riesz-Workspace-Root": "/workspace",
            },
        )
        with mock.patch("bridge.viewer.runner.request", new=mock.AsyncMock(return_value=fake)) as m:
            req = self.rf.get(f"/api/view/{self.tok}/design/p.html")
            resp = await viewer.serve(req, self.tok, "design/p.html")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")
        self.assertEqual(resp["Cross-Origin-Resource-Policy"], "cross-origin")
        self.assertEqual(resp["Cache-Control"], "private, no-cache")
        self.assertIn("allow-downloads", resp["Content-Security-Policy"])
        self.assertNotIn("X-Riesz-Project-Root", resp)
        text = resp.content.decode("utf-8")
        self.assertIn(f'/api/view/{quote(self.tok, safe="")}/img/a.webp?__rz_from=design', text)
        self.assertIn('src="app.js"', text)
        m.assert_awaited_once()

    async def test_range_206_passthrough(self):
        fake = _FakeResp(
            206,
            b"0123",
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": "4",
                "Content-Range": "bytes 0-3/100",
                "Accept-Ranges": "bytes",
            },
        )
        with mock.patch("bridge.viewer.runner.request", new=mock.AsyncMock(return_value=fake)) as m:
            req = self.rf.get(f"/api/view/{self.tok}/demo.mp4", HTTP_RANGE="bytes=0-3")
            resp = await viewer.serve(req, self.tok, "demo.mp4")
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp["Content-Range"], "bytes 0-3/100")
        self.assertEqual(resp["Accept-Ranges"], "bytes")
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")
        chunks = [c async for c in resp.streaming_content]
        self.assertEqual(b"".join(chunks), b"0123")
        _, kwargs = m.call_args
        self.assertEqual(kwargs["headers"], {"Range": "bytes=0-3"})

    async def test_abs_token_calls_view_asset_endpoint(self):
        abs_tok = viewer.make_asset_token("/tmp/chart.webp")
        fake = _FakeResp(200, b"WEBP", headers={"Content-Type": "image/webp", "Content-Length": "4"})
        with mock.patch("bridge.viewer.runner.request", new=mock.AsyncMock(return_value=fake)) as m:
            req = self.rf.get(f"/api/view/{abs_tok}/chart.webp")
            resp = await viewer.serve(req, abs_tok, "chart.webp")
        self.assertEqual(resp.status_code, 200)
        args, kwargs = m.call_args
        self.assertEqual(args, ("GET", "/v1/files/view-asset"))
        self.assertEqual(kwargs["params"], {"path": "/tmp/chart.webp"})


if __name__ == "__main__":
    unittest.main()
