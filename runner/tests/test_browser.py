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


if __name__ == "__main__":
    unittest.main()
