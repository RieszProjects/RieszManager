"""Tests for HTML preview local-resource support in runner (files.py, shares.py, server.py)."""

from __future__ import annotations

import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

from riesz_runner import files as files_mod
from riesz_runner.config import Config
from riesz_runner.server import build_app
from riesz_runner.shares import Shares


class FilesAndSharesUnitTest(unittest.TestCase):
    def test_mime_types_cover_web_assets(self):
        self.assertEqual(files_mod.mime_type(Path("a.webp")), "image/webp")
        self.assertEqual(files_mod.mime_type(Path("a.woff2")), "font/woff2")
        self.assertEqual(files_mod.mime_type(Path("a.otf")), "font/otf")
        self.assertEqual(files_mod.mime_type(Path("a.mjs")), "text/javascript")
        self.assertEqual(files_mod.mime_type(Path("a.cjs")), "text/javascript")
        self.assertEqual(files_mod.mime_type(Path("a.wasm")), "application/wasm")
        self.assertEqual(files_mod.mime_type(Path("a.glb")), "model/gltf-binary")
        self.assertEqual(files_mod.mime_type(Path("a.mp3")), "audio/mpeg")

    def test_safe_view_asset_allows_valid_and_blocks_sensitive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "ok.webp"
            img.write_bytes(b"RIFF")
            env = root / ".env"
            env.write_text("SECRET=1")
            env_local = root / ".env.local"
            env_local.write_text("SECRET=2")
            key = root / "priv.key"
            key.write_text("KEY")
            git_dir = root / ".git"
            git_dir.mkdir()
            git_cfg = git_dir / "config"
            git_cfg.write_text("[core]")
            outside = Path(tempfile.mkdtemp())
            try:
                other = outside / "other.png"
                other.write_bytes(b"PNG")
                self.assertEqual(files_mod.safe_view_asset(str(img), [root]), img.resolve())
                for bad in (env, env_local, key, git_cfg, other, Path("/etc/passwd")):
                    with self.assertRaises(files_mod.FileAccessError):
                        files_mod.safe_view_asset(str(bad), [root])
                # allow_external permits non-sensitive outside files, still refuses sensitive ones
                self.assertEqual(files_mod.safe_view_asset(str(other), [root], allow_external=True), other.resolve())
                with self.assertRaises(files_mod.FileAccessError):
                    files_mod.safe_view_asset(str(env), [root], allow_external=True)
            finally:
                for p in outside.iterdir():
                    p.unlink()
                outside.rmdir()

    def test_shares_resolve_raw_serves_siblings_of_sent_html(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            state = base / "state"
            src_dir = base / "demo"
            (src_dir / "img").mkdir(parents=True)
            html_file = src_dir / "index.html"
            html_file.write_text("<img src='img/a.webp'>", encoding="utf-8")
            img_file = src_dir / "img" / "a.webp"
            img_file.write_bytes(b"WEBP")
            secret = src_dir / ".env"
            secret.write_text("TOKEN=1", encoding="utf-8")
            outside = base / "escape.txt"
            outside.write_text("nope", encoding="utf-8")

            sid = "11111111-1111-1111-1111-111111111111"
            shares = Shares(state)
            entry = shares.share(sid, str(html_file))
            key = entry["ref"].split("/", 1)[0]

            # The shared HTML itself comes from the snapshot copy, with orig_path pointing to the real file.
            path, orig, found_key = shares.resolve_raw(sid, entry["ref"])
            self.assertEqual(path.read_text(encoding="utf-8"), "<img src='img/a.webp'>")
            self.assertEqual(orig, html_file.resolve())
            self.assertEqual(found_key, key)

            # Relative sibling resource resolves to orig_dir/img/a.webp.
            sib_path, sib_orig, sib_key = shares.resolve_raw(sid, f"{key}/img/a.webp")
            self.assertEqual(sib_path, img_file.resolve())
            self.assertEqual(sib_orig, img_file.resolve())
            self.assertEqual(sib_key, key)

            # Sensitive sibling and parent escape are refused.
            with self.assertRaises(files_mod.FileAccessError):
                shares.resolve_raw(sid, f"{key}/.env")
            with self.assertRaises(files_mod.FileAccessError):
                shares.resolve_raw(sid, f"{key}/../escape.txt")


class RunnerFilesViewerHttpTest(AioHTTPTestCase):
    async def get_application(self):
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        self.ws = base / "ws"
        self.proj = self.ws / "MyProj"
        (self.proj / "dist" / "assets").mkdir(parents=True)
        (self.proj / "design").mkdir(parents=True)
        (self.proj / "dist" / "index.html").write_text("<script src='/assets/app.js'></script>", encoding="utf-8")
        (self.proj / "dist" / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")
        (self.proj / "design" / "中文页.html").write_text("<h1>你好</h1>", encoding="utf-8")

        self.tmp_asset_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="rz-view-test-"))
        self.tmp_asset = self.tmp_asset_dir / "chart.webp"
        self.tmp_asset.write_bytes(b"RIFFWEBP")

        cfg = Config(
            workspace_root=self.ws.resolve(),
            claude_home=base / "claude",
            claude_bin="claude",
            state_dir=base / "state",
            socket_path=base / "runner.sock",
            token="t" * 32,
            browser_enabled=False,
        )
        self.cfg = cfg
        app = build_app(cfg)
        return app

    async def tearDownAsync(self):
        await super().tearDownAsync()
        self.tmp_asset.unlink(missing_ok=True)
        self.tmp_asset_dir.rmdir()
        self._td.cleanup()

    def _auth(self):
        return {"Authorization": f"Bearer {self.cfg.token}"}

    async def test_files_raw_headers_unicode_and_from_fallback(self):
        # Unicode filename must not break HTTP response headers.
        resp = await self.client.get(
            "/v1/files/raw",
            params={"project": "MyProj", "path": "design/中文页.html"},
            headers=self._auth(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            urllib.parse.unquote(resp.headers["X-Riesz-File-Path"]),
            str((self.proj / "design" / "中文页.html").resolve()),
        )
        self.assertEqual(
            urllib.parse.unquote(resp.headers["X-Riesz-Project-Root"]),
            str(self.proj.resolve()),
        )

        # Root-relative fallback with `from=dist/assets` resolves dist/assets/app.js when root/assets/app.js doesn't exist.
        resp2 = await self.client.get(
            "/v1/files/raw",
            params={"project": "MyProj", "path": "assets/app.js", "from": "dist/assets"},
            headers=self._auth(),
        )
        self.assertEqual(resp2.status, 200)
        self.assertEqual(await resp2.text(), "console.log(1);")

    async def test_files_raw_shared_siblings_and_session_shared_workspace_info(self):
        from riesz_runner.server import HUB
        hub = self.app[HUB]
        sid = "22222222-2222-2222-2222-222222222222"

        # Share an external HTML file in /tmp with a sibling asset.
        ext_html = self.tmp_asset_dir / "report.html"
        ext_html.write_text("<img src='chart.webp'>", encoding="utf-8")
        try:
            entry = hub.shares.share(sid, str(ext_html))
            key = entry["ref"].split("/", 1)[0]
            resp = await self.client.get(
                "/v1/files/raw",
                params={"project": f"@shared/{sid}", "path": f"{key}/chart.webp"},
                headers=self._auth(),
            )
            self.assertEqual(resp.status, 200)
            self.assertEqual(await resp.read(), b"RIFFWEBP")
            self.assertEqual(resp.headers.get("X-Riesz-View-Prefix"), key)

            # Share a workspace HTML file and confirm session_shared exposes workspaceProject/workspacePath.
            hub.shares.share(sid, str(self.proj / "design" / "中文页.html"))
            sresp = await self.client.get(f"/v1/sessions/{sid}/shared", headers=self._auth())
            self.assertEqual(sresp.status, 200)
            data = await sresp.json()
            ws_item = next(f for f in data["files"] if f["name"] == "中文页.html")
            self.assertEqual(ws_item["workspaceProject"], "MyProj")
            self.assertEqual(ws_item["workspacePath"], "design/中文页.html")
        finally:
            ext_html.unlink(missing_ok=True)

    async def test_files_view_asset_allows_tmp_and_refuses_sensitive(self):
        resp = await self.client.get(
            "/v1/files/view-asset",
            params={"path": str(self.tmp_asset)},
            headers=self._auth(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), b"RIFFWEBP")
        self.assertEqual(resp.headers.get("Content-Type"), "image/webp")

        bad = await self.client.get(
            "/v1/files/view-asset",
            params={"path": "/etc/passwd"},
            headers=self._auth(),
        )
        self.assertEqual(bad.status, 404)
