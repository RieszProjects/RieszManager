"""Real Textual Pilot UI tests for `riesz-node` multi-panel TUI."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from textual.widgets import DataTable, Input, ListView, RichLog, Static

from riesz_node import ed25519_pure as ed
from riesz_node.agent import BoundSession, CommandEntry, LocalConfirm, NodeAgent, PendingApproval, PendingBind
from riesz_node.tui import EditDirsScreen, NodeTUI


class NodeTUITest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        seed, pub = ed.generate_keypair()
        self.device = {
            "nodeId": "deadbeef12345678",
            "name": "win-pc",
            "server": "https://example.test",
            "seed": ed.b64u_encode(seed),
            "publicKey": ed.b64u_encode(pub),
        }
        self.sent: list[dict] = []

    async def asyncTearDown(self) -> None:
        self.td.cleanup()

    def _make_agent(self) -> NodeAgent:
        agent = NodeAgent(device=self.device, roots=[self.root], max_mode="auto", interactive=True)
        agent.status = "online"
        agent.status_detail = "已连接"

        async def fake_send(payload):
            self.sent.append(payload)

        agent._send = fake_send  # type: ignore[method-assign]
        return agent

    async def test_tui_bind_approval_local_confirm_and_streaming_output(self):
        agent = self._make_agent()
        app = NodeTUI(agent, auto_connect=False)
        async with app.run_test(size=(120, 36)) as pilot:
            # 1. Incoming bind request -> prompt bar + dir input visible -> invalid dir rejected -> valid dir accepted
            sub_dir = self.root / "session_dir"
            sub_dir.mkdir()
            agent.pending_binds.append(PendingBind(
                bind_id="b1", session_id="s1", session_title="项目构建", harness="claude",
                project="demo", mode="auto", created_at=time.time(),
            ))
            agent._emit()
            await pilot.pause(0.15)
            bar = app.query_one("#prompt-bar")
            self.assertTrue(bar.has_class("visible"))
            dir_input = app.query_one("#bind-dirs-input", Input)
            # Non-existent dir shows inline error and keeps bind pending
            dir_input.value = str(self.root / "does_not_exist")
            await pilot.click("#btn-allow")
            await pilot.pause(0.12)
            self.assertNotIn("s1", agent.bindings)
            self.assertTrue(app.query_one("#prompt-error").has_class("visible"))
            # Valid session-specific dir succeeds and sends dirs in bind_response
            dir_input.value = str(sub_dir)
            await pilot.click("#btn-allow")
            await pilot.pause(0.15)
            self.assertIn("s1", agent.bindings)
            self.assertEqual(agent.bindings["s1"].dirs, [sub_dir.resolve()])
            self.assertEqual(self.sent[-1]["type"], "bind_response")
            self.assertEqual(self.sent[-1]["dirs"], [str(sub_dir.resolve())])
            self.assertTrue(self.sent[-1]["allow"])
            self.assertFalse(bar.has_class("visible"))

            # 2. Normal approval -> press 'n' rejects
            agent.pending_approvals.append(PendingApproval(
                approval_id="a1", session_id="s1", session_title="项目构建", action="exec",
                command="npm publish", cwd=str(self.root), risk="high", reason="发布包", created_at=time.time(),
            ))
            agent._emit()
            await pilot.pause(0.15)
            self.assertTrue(bar.has_class("visible"))
            await pilot.press("n")
            await pilot.pause(0.15)
            self.assertEqual(self.sent[-1]["type"], "approval_response")
            self.assertFalse(self.sent[-1]["allow"])

            # 3. Local high-risk confirm -> danger banner -> press 'y' resolves future
            fut = asyncio.get_running_loop().create_future()
            agent.pending_local_confirms.append(LocalConfirm(
                confirm_id="lc1", session_id="s1", session_title="项目构建",
                command="sudo whoami", cwd=str(self.root), reason="涉及提权命令",
                created_at=time.time(), future=fut,
            ))
            agent._emit()
            await pilot.pause(0.15)
            self.assertTrue(bar.has_class("danger"))
            await pilot.press("y")
            await pilot.pause(0.15)
            self.assertTrue(fut.done() and fut.result() is True)

            # 4. Streaming commands render without resetting table cursor
            c1 = CommandEntry(
                cmd_id="c1", session_id="s1", session_title="项目构建", action="exec",
                command="pytest", cwd=str(self.root), risk="low", risk_reason="单测",
                status="done", started_at=time.time(), exit_code=0, output="passed\n",
            )
            c2 = CommandEntry(
                cmd_id="c2", session_id="s1", session_title="项目构建", action="exec",
                command="build.sh", cwd=str(self.root), risk="low", risk_reason="构建",
                status="running", started_at=time.time(), output="line 1\n",
            )
            agent._record_cmd(c1)
            agent._record_cmd(c2)
            agent._emit()
            await pilot.pause(0.15)
            table = app.query_one("#commands", DataTable)
            self.assertEqual(table.row_count, 2)
            # Select c1 (row 1), then stream new output to c2: selection must NOT jump away from c1!
            app.selected_cmd_id = "c1"
            app._render_output()
            c2.output += "line 2\nline 3\n"
            agent._emit()
            await pilot.pause(0.15)
            self.assertEqual(app.selected_cmd_id, "c1")

            # 5. Press 'u' on selected session unbinds it
            app.selected_session = "s1"
            await pilot.press("u")
            await pilot.pause(0.15)
            self.assertNotIn("s1", agent.bindings)
            self.assertEqual(self.sent[-1]["type"], "unbind")

    async def test_tui_mouse_buttons_kill_and_quit(self):
        agent = self._make_agent()
        killed_flag = {"called": False}

        class FakeProc:
            async def kill(self):
                killed_flag["called"] = True

        app = NodeTUI(agent, auto_connect=False)
        async with app.run_test(size=(120, 36)) as pilot:
            # Mouse click #btn-allow on bind request
            agent.pending_binds.append(PendingBind(
                bind_id="b2", session_id="s2", session_title="鼠标测试", harness="antigravity",
                project="demo", mode="auto", created_at=time.time(),
            ))
            agent._emit()
            await pilot.pause(0.15)
            await pilot.click("#btn-allow")
            await pilot.pause(0.15)
            self.assertIn("s2", agent.bindings)

            # Press 'k' kills the currently selected running command
            c_run = CommandEntry(
                cmd_id="c-run", session_id="s2", session_title="鼠标测试", action="exec",
                command="sleep 60", cwd=str(self.root), risk="low", risk_reason="",
                status="running", started_at=time.time(), proc=FakeProc(),  # type: ignore[arg-type]
            )
            agent._record_cmd(c_run)
            agent._emit()
            await pilot.pause(0.15)
            app.selected_cmd_id = "c-run"
            await pilot.press("k")
            await pilot.pause(0.15)
            self.assertTrue(killed_flag["called"])

            # Press Ctrl+Q cleanly exits
            await pilot.press("ctrl+q")
            await pilot.pause(0.1)

    async def test_tui_edit_session_dirs_modal(self):
        agent = self._make_agent()
        d1 = self.root / "d1"
        d2 = self.root / "d2"
        d1.mkdir()
        d2.mkdir()
        agent.bindings["s3"] = BoundSession(
            session_id="s3", title="目录编辑测试", harness="claude", project="p",
            mode="auto", bound_at=time.time(), dirs=[d1],
        )
        app = NodeTUI(agent, auto_connect=False)
        async with app.run_test(size=(120, 36)) as pilot:
            app.selected_session = "s3"
            await pilot.press("e")
            await pilot.pause(0.15)
            self.assertIsInstance(app.screen, EditDirsScreen)
            app.screen.query_one("#edit-input", Input).value = f"{d1}; {d2}"
            await pilot.click("#edit-save")
            await pilot.pause(0.15)
            self.assertEqual(agent.bindings["s3"].dirs, [d1.resolve(), d2.resolve()])
            self.assertEqual(self.sent[-1]["type"], "bind_update")
            self.assertEqual(self.sent[-1]["dirs"], [str(d1.resolve()), str(d2.resolve())])


if __name__ == "__main__":
    unittest.main()
