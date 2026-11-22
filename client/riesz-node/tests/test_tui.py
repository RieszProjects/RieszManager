"""Interactive Textual Pilot tests for single-session `riesz-node` TUI."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from textual.widgets import Input, RichLog, Static

from riesz_node import ed25519_pure as ed
from riesz_node.agent import CommandEntry, LocalConfirm, NodeAgent, PendingApproval
from riesz_node.tui import DirPickerScreen, EditDirsScreen, ModeScreen, NodeTUI, RenameScreen


class NodeTUITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.orig_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.tmp.name) / "cfg")
        self.addCleanup(self._restore_xdg)
        self.sent: list[dict] = []

    def _restore_xdg(self):
        if self.orig_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.orig_xdg

    def _make_agent(self, dirs: list[Path] | None = None) -> NodeAgent:
        seed, pub = ed.generate_keypair()
        session = {
            "nodeId": "deadbeef1234",
            "name": "test-pc",
            "server": "https://example.com",
            "seed": ed.b64u_encode(seed),
            "publicKey": ed.b64u_encode(pub),
            "sessionId": "11111111-2222-4333-8444-555555555555",
            "sessionTitle": "demo-session",
            "harness": "antigravity",
            "project": "demo",
            "mode": "auto",
        }
        agent = NodeAgent(session=session, dirs=[self.root] if dirs is None else dirs, interactive=True)
        agent.status = "online"
        agent.status_detail = "已连接"

        async def fake_send(payload: dict) -> None:
            self.sent.append(payload)

        agent._send = fake_send  # type: ignore[method-assign]
        return agent

    async def test_tui_approval_local_confirm_and_streaming_output(self):
        agent = self._make_agent()
        app = NodeTUI(agent, auto_connect=False)
        async with app.run_test(size=(120, 36)) as pilot:
            status_txt = str(app.query_one("#status-bar", Static).render())
            self.assertIn("demo-session", status_txt)
            self.assertIn("test-pc", status_txt)

            # 1. Pending command approval -> press 'n' rejects
            agent.pending_approvals.append(PendingApproval(
                approval_id="a1", action="exec", command="python3 deploy.py",
                cwd=str(self.root), risk="medium", reason="需要确认", created_at=time.time(),
            ))
            agent._emit()
            await pilot.pause(0.15)
            await pilot.press("n")
            await pilot.pause(0.15)
            self.assertEqual(self.sent[-1]["type"], "approval_response")
            self.assertFalse(self.sent[-1]["allow"])

            # 2. Local high-risk confirm -> press 'y' resolves future True
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            agent.pending_local_confirms.append(LocalConfirm(
                confirm_id="lc1", command="sudo id", cwd=str(self.root),
                reason="提权命令", created_at=time.time(), future=fut,
            ))
            agent._emit()
            await pilot.pause(0.15)
            self.assertTrue(app.query_one("#prompt-bar").has_class("danger"))
            await pilot.press("y")
            await pilot.pause(0.15)
            self.assertTrue(fut.done() and fut.result())

            # 3. Streaming command output increments RichLog cleanly
            entry = CommandEntry(
                cmd_id="c1", action="exec", command="echo hi", cwd=str(self.root),
                risk="low", risk_reason="", status="running", started_at=time.time(), output="line1\n",
            )
            agent._record_cmd(entry)
            agent._emit()
            await pilot.pause(0.15)
            entry.output += "line2\n"
            agent._emit()
            await pilot.pause(0.15)
            log_w = app.query_one("#output", RichLog)
            self.assertGreaterEqual(len(log_w.lines), 4)

    async def test_tui_multi_dir_picker_and_rename(self):
        sub = self.root / "nested" / "leaf"
        sub.mkdir(parents=True)
        other = self.root / "other"
        other.mkdir()
        agent = self._make_agent(dirs=[sub])

        app = NodeTUI(agent, auto_connect=False)
        async with app.run_test(size=(120, 36)) as pilot:
            # Open EditDirsScreen -> DirPickerScreen -> go UP twice -> append -> add other -> save
            await pilot.click("#btn-edit-dirs")
            await pilot.pause(0.15)
            self.assertIsInstance(app.screen, EditDirsScreen)
            await pilot.click("#edit-browse")
            await pilot.pause(0.15)
            self.assertIsInstance(app.screen, DirPickerScreen)
            await pilot.click("#pick-up")
            await pilot.pause(0.1)
            await pilot.click("#pick-up")
            await pilot.pause(0.1)
            self.assertEqual(app.screen.tree_root, self.root)
            await pilot.click("#pick-add")
            await pilot.pause(0.15)
            self.assertIsInstance(app.screen, EditDirsScreen)
            app.screen.query_one("#edit-input", Input).value = str(other)
            await pilot.click("#edit-save")
            await pilot.pause(0.15)
            self.assertEqual(agent.dirs, [sub.resolve(), self.root.resolve(), other.resolve()])
            self.assertEqual(self.sent[-1]["type"], "dirs_update")

            # Switch permission mode via 'm' -> ModeScreen -> updates local mode & sends mode_update
            await pilot.press("m")
            await pilot.pause(0.15)
            self.assertIsInstance(app.screen, ModeScreen)
            await pilot.click("#mode-full")
            await pilot.pause(0.15)
            self.assertEqual(agent.mode, "full")
            self.assertEqual(self.sent[-1], {"type": "mode_update", "mode": "full"})

            # Rename via 'r' -> RenameScreen -> sends rename event & updates local name
            await pilot.press("r")
            await pilot.pause(0.15)
            self.assertIsInstance(app.screen, RenameScreen)
            app.screen.query_one("#rename-input", Input).value = "my-gpu-box"
            await pilot.click("#rename-ok")
            await pilot.pause(0.15)
            self.assertEqual(agent.name, "my-gpu-box")
            self.assertEqual(self.sent[-1], {"type": "rename", "name": "my-gpu-box"})

    async def test_tui_kill_and_quit(self):
        agent = self._make_agent()
        killed_flag = {"called": False}

        class FakeProc:
            async def kill(self):
                killed_flag["called"] = True

        app = NodeTUI(agent, auto_connect=False)
        async with app.run_test(size=(120, 36)) as pilot:
            c_run = CommandEntry(
                cmd_id="c-run", action="exec", command="sleep 60", cwd=str(self.root),
                risk="low", risk_reason="", status="running", started_at=time.time(),
                proc=FakeProc(),  # type: ignore[arg-type]
            )
            agent._record_cmd(c_run)
            agent._emit()
            await pilot.pause(0.15)
            app.selected_cmd_id = "c-run"
            await pilot.press("k")
            await pilot.pause(0.15)
            self.assertTrue(killed_flag["called"])
            await pilot.press("ctrl+q")
            await pilot.pause(0.1)


if __name__ == "__main__":
    unittest.main()
