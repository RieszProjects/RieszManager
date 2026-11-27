"""Tests for the dedicated server browser and multi-MCP mounting (browser.py)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from riesz_runner.browser import ServerBrowser, mcp_server, normalize_url
from riesz_runner.config import Config
from riesz_runner.drivers.acp import AcpDriver
from riesz_runner.drivers.base import ToolServer
from riesz_runner.drivers.codex import CodexDriver
from riesz_runner.drivers.dsh import DshDriver
from riesz_runner.process import ClaudeProcess, claude_mcp_config


def _cfg(state_dir: Path, **overrides) -> Config:
    return Config(
        workspace_root=state_dir,
        claude_home=state_dir / "claude",
        claude_bin="claude",
        state_dir=state_dir,
        socket_path=state_dir / "runner.sock",
        token="x" * 32,
        **overrides,
    )


class BrowserConfigAndMountTest(unittest.TestCase):
    def test_normalize_url(self) -> None:
        self.assertEqual(normalize_url(""), "about:blank")
        self.assertEqual(normalize_url("example.com"), "https://example.com")
        self.assertEqual(normalize_url("arxiv.org/abs/2401.00001"), "https://arxiv.org/abs/2401.00001")
        self.assertEqual(normalize_url("localhost:3000/test"), "http://localhost:3000/test")
        self.assertEqual(normalize_url("127.0.0.1:8080/a"), "http://127.0.0.1:8080/a")
        self.assertEqual(normalize_url("http://127.0.0.1:8080/a"), "http://127.0.0.1:8080/a")
        self.assertEqual(normalize_url("about:blank"), "about:blank")
        self.assertEqual(normalize_url("hello world"), "https://www.google.com/search?q=hello+world")
        self.assertEqual(normalize_url("my-server:8080/x"), "http://my-server:8080/x")
        self.assertEqual(normalize_url("nas.local:5000"), "http://nas.local:5000")
        self.assertEqual(normalize_url("mailto:a@example.com"), "mailto:a@example.com")
        self.assertEqual(normalize_url("python"), "https://www.google.com/search?q=python")
        self.assertEqual(normalize_url("overleaf"), "https://www.google.com/search?q=overleaf")
        self.assertEqual(normalize_url("python3.12"), "https://www.google.com/search?q=python3.12")
        self.assertEqual(normalize_url("量子力学"), "https://www.google.com/search?q=%E9%87%8F%E5%AD%90%E5%8A%9B%E5%AD%A6")

    def test_mcp_server_builds_tool_server(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td), browser_port=19333)
            with patch("riesz_runner.browser.find_chrome", return_value="/usr/bin/google-chrome"), \
                 patch("riesz_runner.browser.find_mcp_bin", return_value="/opt/node/bin/chrome-devtools-mcp"), \
                 patch("os.access", return_value=True):
                srv = mcp_server(cfg)
            assert srv is not None
            self.assertEqual(srv.name, "browser")
            self.assertEqual(srv.command, "/opt/node/bin/node")
            self.assertIn("--browserUrl", srv.args)
            self.assertIn("http://127.0.0.1:19333", srv.args)
            self.assertIn("--usageStatistics=false", srv.args)

    def test_mcp_server_none_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(Path(td), browser_enabled=False)
            self.assertIsNone(mcp_server(cfg))

    def test_extra_tools_mounted_in_every_harness(self) -> None:
        riesz = ToolServer(name="riesz", command="/bin/py", args=("-I", "relay.py"))
        browser = ToolServer(name="browser", command="/bin/node", args=("mcp.js", "--browserUrl", "http://127.0.0.1:19222"))

        # Claude Code
        cfg = claude_mcp_config(riesz, browser)
        self.assertEqual(set(cfg["mcpServers"]), {"riesz", "browser"})
        with tempfile.TemporaryDirectory() as td:
            proc = ClaudeProcess(_cfg(Path(td)), session_id="s1", cwd=td, sink=None, tools=riesz, extra_tools=(browser,))
            cmd = proc._command()
            idx = cmd.index("--mcp-config")
            self.assertIn('"browser"', cmd[idx + 1])
            self.assertIn('"riesz"', cmd[idx + 1])

        # Codex
        codex = CodexDriver(cwd="/tmp", sink=None, tools=riesz, extra_tools=(browser,))
        self.assertEqual([s.name for s in codex.all_tools], ["riesz", "browser"])
        mounted = [a for s in codex.all_tools for a in codex.mount_tools(s)]
        self.assertTrue(any("mcp_servers.browser.command" in a for a in mounted))
        self.assertTrue(any("mcp_servers.riesz.command" in a for a in mounted))

        # dsh
        dsh = DshDriver(cwd="/tmp", sink=None, tools=riesz, extra_tools=(browser,))
        self.assertIn("mcp-browser", dsh.mount_tools(browser))
        self.assertEqual([s.name for s in dsh.all_tools], ["riesz", "browser"])

        # ACP (Gemini / Antigravity)
        acp = AcpDriver(cwd="/tmp", sink=None, tools=riesz, extra_tools=(browser,))
        names = [e["name"] for e in acp._mcp_servers()]
        self.assertEqual(names, ["riesz", "browser"])


class ServerBrowserStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            b = ServerBrowser(_cfg(Path(td), browser_enabled=False))
            st = await b.status()
            self.assertFalse(st["enabled"])
            self.assertFalse(st["available"])
            self.assertFalse(st["running"])

    async def test_watchdog_restarts_chrome_unless_stopped_by_user(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock

        with tempfile.TemporaryDirectory() as td:
            b = ServerBrowser(_cfg(Path(td)))
            calls = {"start": 0}
            ready_seq = [False, True, False, True]

            async def fake_ready() -> bool:
                return ready_seq.pop(0) if ready_seq else False

            async def fake_ensure() -> None:
                calls["start"] += 1

            b._cdp_ready = fake_ready  # type: ignore[method-assign]
            b.ensure_running = fake_ensure  # type: ignore[method-assign]
            with patch("riesz_runner.browser.WATCHDOG_INTERVAL", 0.01):
                task = asyncio.create_task(b.start_background())
                await asyncio.sleep(0.08)
                # Should have started on boot and auto-restarted after dropping offline
                self.assertGreaterEqual(calls["start"], 2)
                # Explicit user stop suppresses auto-restart
                await b.stop(by_user=True)
                before = calls["start"]
                await asyncio.sleep(0.05)
                self.assertEqual(calls["start"], before)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class ServerBrowserStartFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_chrome_exiting_at_once_reports_its_code_and_stderr(self) -> None:
        import os
        import socket
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake-chrome"
            fake.write_text("#!/bin/sh\necho 'fatal: profile is locked' >&2\nexit 3\n")
            os.chmod(fake, 0o755)
            with socket.socket() as sock:            # a port nothing listens on
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            b = ServerBrowser(_cfg(Path(td), browser_bin=str(fake), browser_port=port, browser_headless="true"))
            with self.assertRaises(RuntimeError) as ctx:
                await b.ensure_running()
            self.assertIn("Chrome exited (3)", str(ctx.exception))
            self.assertIn("profile is locked", b._last_error or "")

    async def test_stderr_drain_survives_a_very_long_line(self) -> None:
        import asyncio
        import sys
        with tempfile.TemporaryDirectory() as td:
            b = ServerBrowser(_cfg(Path(td), browser_enabled=False))
            code = ("import sys; sys.stderr.write('x'*200000+'\\n'); "
                    "[sys.stderr.write('line %d\\n' % i) for i in range(20000)]; sys.stderr.flush(); print('DONE', flush=True)")
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            drain = asyncio.create_task(b._drain_stderr(proc))
            out = await asyncio.wait_for(proc.stdout.readline(), 10)   # would hang if the pipe stopped being read
            self.assertEqual(out, b"DONE\n")
            await proc.wait()
            await asyncio.wait_for(drain, 5)
            self.assertEqual(b._stderr_tail[-1], "line 19999")


if __name__ == "__main__":
    unittest.main()
