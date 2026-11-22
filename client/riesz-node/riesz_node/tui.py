"""Multi-panel Textual TUI for `riesz-node` with per-session interactive directory selection."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, DirectoryTree, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static

from .agent import CommandEntry, NodeAgent
from .policy import PolicyError, parse_dir_list

STATUS_ICON = {"running": "⏳", "confirming": "🔒", "done": "✓", "error": "✗", "killed": "⊘", "denied": "⛔"}
RISK_LABEL = {"low": "低", "medium": "中", "high": "高"}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


class FolderOnlyTree(DirectoryTree):
    def filter_paths(self, paths):  # type: ignore[override]
        return [p for p in paths if p.is_dir()]


class DirPickerScreen(ModalScreen[Path | None]):
    """Interactive folder tree modal to pick an allowed directory without typing paths."""

    CSS = """
    DirPickerScreen { align: center middle; }
    #picker-box {
        width: 82%;
        max-width: 96;
        height: 80%;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #picker-title { text-style: bold; margin-bottom: 1; }
    #picker-current { color: $text-muted; margin-bottom: 1; }
    #picker-tree { height: 1fr; border: solid $panel; margin-bottom: 1; }
    #picker-actions { height: auto; }
    #picker-actions Button { margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, start_path: Path) -> None:
        super().__init__()
        self.start_path = start_path if start_path.is_dir() else Path.home()
        self.chosen: Path = self.start_path

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static("选择要授权给该会话的本地目录（点击或方向键选中）", id="picker-title")
            yield Static(f"当前选中：{self.chosen}", id="picker-current")
            yield FolderOnlyTree(str(self.start_path), id="picker-tree")
            with Horizontal(id="picker-actions"):
                yield Button("选择此目录", variant="success", id="pick-ok")
                yield Button("切到用户主目录", variant="default", id="pick-home")
                yield Button("取消", variant="error", id="pick-cancel")

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.chosen = Path(event.path)
        self.query_one("#picker-current", Static).update(f"当前选中：{self.chosen}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pick-ok":
            self.dismiss(self.chosen)
        elif event.button.id == "pick-home":
            home = Path.home()
            self.chosen = home
            tree = self.query_one("#picker-tree", FolderOnlyTree)
            tree.path = str(home)
            self.query_one("#picker-current", Static).update(f"当前选中：{self.chosen}")
        elif event.button.id == "pick-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditDirsScreen(ModalScreen[list[str] | None]):
    """Modal to edit a bound session's allowed directories at any time."""

    CSS = """
    EditDirsScreen { align: center middle; }
    #edit-box {
        width: 82%;
        max-width: 96;
        height: auto;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #edit-title { text-style: bold; margin-bottom: 1; }
    #edit-input { margin-bottom: 1; }
    #edit-error { color: $error; height: auto; margin-bottom: 1; }
    #edit-actions { height: auto; }
    #edit-actions Button { margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, session_title: str, current_dirs: list[Path]) -> None:
        super().__init__()
        self.session_title = session_title
        self.current_dirs = current_dirs

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Static(f"编辑会话「{self.session_title}」允许访问的目录（多个目录用 ; 分隔）", id="edit-title")
            yield Input(value="; ".join(str(p) for p in self.current_dirs), placeholder="输入目录路径…", id="edit-input")
            yield Static("", id="edit-error")
            with Horizontal(id="edit-actions"):
                yield Button("保存", variant="success", id="edit-save")
                yield Button("浏览添加目录…", variant="primary", id="edit-browse")
                yield Button("取消", variant="error", id="edit-cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "edit-input":
            self._save()

    def _save(self) -> None:
        raw = self.query_one("#edit-input", Input).value
        dirs = parse_dir_list(raw)
        if not dirs:
            self.query_one("#edit-error", Static).update("请至少保留一个目录")
            return
        self.dismiss(dirs)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-save":
            self._save()
        elif event.button.id == "edit-browse":
            start = self.current_dirs[0] if self.current_dirs else Path.cwd()
            self.app.push_screen(DirPickerScreen(start), self._on_picked)
        elif event.button.id == "edit-cancel":
            self.dismiss(None)

    def _on_picked(self, path: Path | None) -> None:
        if path is None:
            return
        inp = self.query_one("#edit-input", Input)
        existing = parse_dir_list(inp.value)
        s = str(path)
        if s not in existing:
            existing.append(s)
        inp.value = "; ".join(existing)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionRow(ListItem):
    def __init__(self, session_id: str | None, text: str) -> None:
        super().__init__(Label(text))
        self.session_id = session_id


class NodeTUI(App[None]):
    TITLE = "Riesz Node · 本地受控节点"
    CSS = """
    Screen { layout: vertical; }
    #status-bar {
        height: 3;
        padding: 0 2;
        background: $surface;
        border-bottom: solid $primary-background;
        content-align: left middle;
    }
    #prompt-bar {
        height: auto;
        padding: 1 2;
        background: $warning 18%;
        border-bottom: solid $warning;
        display: none;
    }
    #prompt-bar.visible { display: block; }
    #prompt-bar.danger { background: $error 22%; border-bottom: solid $error; }
    #prompt-text { margin-bottom: 1; }
    #bind-dir-row { height: auto; margin-bottom: 1; display: none; }
    #bind-dir-row.visible { display: block; }
    #bind-dirs-input { width: 1fr; margin-right: 1; }
    #prompt-error { color: $error; height: auto; display: none; margin-bottom: 1; }
    #prompt-error.visible { display: block; }
    #prompt-actions { height: auto; }
    #prompt-actions Button { margin-right: 1; }
    #main { height: 1fr; }
    #left-pane {
        width: 32%;
        min-width: 26;
        max-width: 48;
        border-right: solid $primary-background;
    }
    .pane-title {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
        text-style: bold;
    }
    #sessions { height: 1fr; }
    #right-pane { width: 1fr; }
    #commands { height: 45%; border-bottom: solid $primary-background; }
    #output { height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        Binding("y", "approve", "同意", show=True),
        Binding("ctrl+y", "approve_force", "确认绑定", show=False),
        Binding("n", "reject", "拒绝", show=True),
        Binding("e", "edit_dirs", "改目录", show=True),
        Binding("k", "kill_cmd", "终止命令", show=True),
        Binding("u", "unbind_session", "解绑会话", show=True),
        Binding("ctrl+q", "quit_node", "断开并退出", show=True),
    ]

    def __init__(self, agent: NodeAgent, *, auto_connect: bool = True) -> None:
        super().__init__()
        self.agent = agent
        self.auto_connect = auto_connect
        self.selected_session: str | None = None
        self.selected_cmd_id: str | None = None
        self._visible_cmds: list[CommandEntry] = []
        self._agent_task: asyncio.Task[None] | None = None
        self._refresh_pending = False
        self._last_sessions_sig: tuple | None = None
        self._last_cmds_sig: tuple | None = None
        self._active_bind_id: str | None = None
        self._rendered_cmd_id: str | None = None
        self._rendered_header_sig: tuple | None = None
        self._rendered_out_len: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status-bar")
        with Vertical(id="prompt-bar"):
            yield Static("", id="prompt-text")
            with Horizontal(id="bind-dir-row"):
                yield Input(placeholder="授权给本会话的目录（多个用 ; 分隔）", id="bind-dirs-input")
                yield Button("浏览目录…", variant="primary", id="btn-browse")
            yield Static("", id="prompt-error")
            with Horizontal(id="prompt-actions"):
                yield Button("同意 (y / Ctrl+Y)", variant="success", id="btn-allow")
                yield Button("拒绝 (n)", variant="error", id="btn-deny")
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                yield Static("已绑定会话 (e 改目录 · u 解绑)", classes="pane-title")
                yield ListView(id="sessions")
            with Vertical(id="right-pane"):
                yield Static("指令与文件传输记录 (k 终止运行中命令)", classes="pane-title")
                yield DataTable(id="commands", cursor_type="row")
                yield Static("实时输出详情", classes="pane-title")
                yield RichLog(id="output", highlight=False, markup=False, wrap=True)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#commands", DataTable)
        table.add_columns("时间", "状态", "会话", "风险", "命令 / 传输", "退出码")
        self.agent.on_update = self._schedule_refresh
        self._refresh_all()
        self.query_one("#sessions", ListView).focus()
        if self.auto_connect:
            self._agent_task = asyncio.create_task(self.agent.run())

    def _schedule_refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        try:
            self.set_timer(0.08, self._flush_refresh)
        except Exception:
            self._refresh_pending = False

    def _flush_refresh(self) -> None:
        self._refresh_pending = False
        self._refresh_all()

    def _set_prompt_error(self, msg: str) -> None:
        err = self.query_one("#prompt-error", Static)
        err.update(msg)
        if msg:
            err.add_class("visible")
        else:
            err.remove_class("visible")

    def _refresh_all(self) -> None:
        # 1. Status bar
        dot = {"online": "🟢 在线", "connecting": "🟡 连接中", "kicked": "🔴 已断开"}.get(self.agent.status, "⚪ 离线")
        preset = ", ".join(str(r) for r in self.agent.roots) if self.agent.roots else "按会话单独授权"
        self.query_one("#status-bar", Static).update(
            f"{dot}  |  节点: {self.agent.name}  |  权限上限: {self.agent.max_mode}  |  默认目录: {preset}\n"
            f"服务器: {self.agent.server}  —  {self.agent.status_detail}"
        )

        # 2. Prompt bar (local high-risk confirm > bind requests > normal approvals)
        bar = self.query_one("#prompt-bar", Vertical)
        text_w = self.query_one("#prompt-text", Static)
        dir_row = self.query_one("#bind-dir-row", Horizontal)
        bar.remove_class("danger")
        dir_row.remove_class("visible")

        if self.agent.pending_local_confirms:
            self._active_bind_id = None
            self._set_prompt_error("")
            lc = self.agent.pending_local_confirms[0]
            text_w.update(
                f"【本机安全拦截 · 必须在本机确认】会话「{lc.session_title}」\n"
                f"命令：{lc.command}\n原因：{lc.reason}"
            )
            bar.add_class("visible")
            bar.add_class("danger")
        elif self.agent.pending_binds:
            b = self.agent.pending_binds[0]
            verb = "申请调整权限为" if b.is_update else "申请绑定本机，权限模式"
            text_w.update(
                f"【会话绑定请求】会话「{b.session_title}」({b.harness or 'agent'}) {verb}：{b.mode}\n"
                f"请为该会话指定允许操作的本地目录（不同会话目录互相隔离）："
            )
            if self._active_bind_id != b.bind_id:
                self._active_bind_id = b.bind_id
                self._set_prompt_error("")
                existing = self.agent.bindings.get(b.session_id)
                defaults = (
                    existing.dirs if existing and existing.dirs
                    else self.agent.roots if self.agent.roots
                    else [Path.cwd()]
                )
                self.query_one("#bind-dirs-input", Input).value = "; ".join(str(p) for p in defaults)
            dir_row.add_class("visible")
            bar.add_class("visible")
        elif self.agent.pending_approvals:
            self._active_bind_id = None
            self._set_prompt_error("")
            a = self.agent.pending_approvals[0]
            text_w.update(
                f"【指令审批 · 风险:{RISK_LABEL.get(a.risk, a.risk)}】会话「{a.session_title}」\n"
                f"命令：{a.command}\n原因：{a.reason}"
            )
            bar.add_class("visible")
        else:
            self._active_bind_id = None
            self._set_prompt_error("")
            bar.remove_class("visible")
            if isinstance(self.focused, Input) and self.focused.id == "bind-dirs-input":
                self.query_one("#sessions", ListView).focus()

        # 3. Sessions list — rebuild ONLY when bindings/dirs/running counts change
        total_running = sum(1 for c in self.agent.commands if c.status == "running")
        sessions_sig = (
            self.selected_session,
            total_running,
            tuple(
                (b.session_id, b.title, b.mode, tuple(str(d) for d in b.dirs),
                 sum(1 for c in self.agent.commands if c.session_id == b.session_id and c.status == "running"))
                for b in self.agent.bindings.values()
            ),
        )
        if sessions_sig != self._last_sessions_sig:
            self._last_sessions_sig = sessions_sig
            lv = self.query_one("#sessions", ListView)
            lv.clear()
            lv.append(SessionRow(None, f"全部会话 ({len(self.agent.bindings)} 绑定 / {total_running} 运行中)"))
            for b in self.agent.bindings.values():
                rc = sum(1 for c in self.agent.commands if c.session_id == b.session_id and c.status == "running")
                prefix = "▶ " if rc else "• "
                dir_hint = b.dirs[0].name or str(b.dirs[0]) if b.dirs else "无目录"
                if len(b.dirs) > 1:
                    dir_hint += f"+{len(b.dirs) - 1}"
                lv.append(SessionRow(b.session_id, f"{prefix}{b.title[:16]} [{b.mode}] 📁{dir_hint}"))

        # 4. Commands table — rebuild ONLY when rows or statuses change
        self._visible_cmds = [
            c for c in self.agent.commands
            if self.selected_session is None or c.session_id == self.selected_session
        ]
        cmds_sig = tuple((c.cmd_id, c.status, c.exit_code, c.risk) for c in self._visible_cmds)
        if cmds_sig != self._last_cmds_sig:
            self._last_cmds_sig = cmds_sig
            table = self.query_one("#commands", DataTable)
            table.clear()
            target_row = 0
            for idx, c in enumerate(self._visible_cmds):
                icon = STATUS_ICON.get(c.status, "•")
                code = "" if c.exit_code is None else str(c.exit_code)
                table.add_row(
                    _fmt_time(c.started_at), icon, c.session_title[:14],
                    RISK_LABEL.get(c.risk, c.risk), c.command[:72], code,
                    key=c.cmd_id,
                )
                if c.cmd_id == self.selected_cmd_id:
                    target_row = idx
            if self._visible_cmds:
                table.move_cursor(row=target_row, animate=False)

        # 5. Selected command output — incremental append
        self._render_output()

    def _render_output(self) -> None:
        log_w = self.query_one("#output", RichLog)
        entry = None
        if self.selected_cmd_id:
            entry = next((c for c in self._visible_cmds if c.cmd_id == self.selected_cmd_id), None)
        if entry is None and self._visible_cmds:
            entry = self._visible_cmds[0]
            self.selected_cmd_id = entry.cmd_id
        if entry is None:
            if self._rendered_cmd_id != "__empty__":
                self._rendered_cmd_id = "__empty__"
                self._rendered_header_sig = None
                self._rendered_out_len = 0
                log_w.clear()
                log_w.write("暂无执行记录。在 Riesz Manager 网页中将本节点绑定到某个会话并在此授权目录后，指令输出会实时显示在这里。")
            return

        header_sig = (entry.cmd_id, entry.status, entry.cwd, entry.risk, entry.risk_reason)
        if self._rendered_cmd_id == entry.cmd_id and self._rendered_header_sig == header_sig and len(entry.output) >= self._rendered_out_len:
            delta = entry.output[self._rendered_out_len:]
            if delta:
                log_w.write(delta)
                self._rendered_out_len = len(entry.output)
            return

        self._rendered_cmd_id = entry.cmd_id
        self._rendered_header_sig = header_sig
        self._rendered_out_len = len(entry.output)
        log_w.clear()
        log_w.write(f"会话：{entry.session_title}  |  状态：{entry.status}  |  目录：{entry.cwd}")
        log_w.write(f"命令：{entry.command}")
        if entry.risk_reason:
            log_w.write(f"风控（{RISK_LABEL.get(entry.risk, entry.risk)}）：{entry.risk_reason}")
        log_w.write("─" * 60)
        log_w.write(entry.output or "（尚无输出）")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionRow):
            self.selected_session = event.item.session_id
            self.selected_cmd_id = None
            self._last_cmds_sig = None
            self._refresh_all()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.selected_cmd_id = str(event.row_key.value)
            self._render_output()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self.selected_cmd_id = str(event.row_key.value)
            self._render_output()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "bind-dirs-input" and self.agent.pending_binds:
            await self.action_approve_force()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-allow":
            await self.action_approve_force()
        elif event.button.id == "btn-deny":
            await self._do_reject()
        elif event.button.id == "btn-browse":
            inp = self.query_one("#bind-dirs-input", Input)
            existing = parse_dir_list(inp.value)
            start = Path(os.path.expanduser(existing[0])) if existing else Path.cwd()
            self.push_screen(DirPickerScreen(start), self._on_bind_dir_picked)

    def _on_bind_dir_picked(self, path: Path | None) -> None:
        if path is None:
            return
        inp = self.query_one("#bind-dirs-input", Input)
        existing = parse_dir_list(inp.value)
        s = str(path)
        if s not in existing:
            existing.append(s)
        inp.value = "; ".join(existing)
        self._set_prompt_error("")

    def _is_typing(self) -> bool:
        if not isinstance(self.focused, Input):
            return False
        if self.focused.id == "bind-dirs-input":
            return self.query_one("#bind-dir-row", Horizontal).has_class("visible")
        return True

    async def action_approve(self) -> None:
        if self._is_typing():
            return
        await self.action_approve_force()

    async def action_approve_force(self) -> None:
        if self.agent.pending_local_confirms:
            await self.agent.respond_local_confirm(self.agent.pending_local_confirms[0], allow=True)
        elif self.agent.pending_binds:
            raw = self.query_one("#bind-dirs-input", Input).value
            dirs = parse_dir_list(raw)
            try:
                await self.agent.respond_bind(self.agent.pending_binds[0], allow=True, dirs=dirs)
                self._set_prompt_error("")
            except PolicyError as exc:
                self._set_prompt_error(str(exc))
        elif self.agent.pending_approvals:
            await self.agent.respond_approval(self.agent.pending_approvals[0], allow=True)

    async def _do_reject(self) -> None:
        self._set_prompt_error("")
        if self.agent.pending_local_confirms:
            await self.agent.respond_local_confirm(self.agent.pending_local_confirms[0], allow=False)
        elif self.agent.pending_binds:
            await self.agent.respond_bind(self.agent.pending_binds[0], allow=False)
        elif self.agent.pending_approvals:
            await self.agent.respond_approval(self.agent.pending_approvals[0], allow=False)

    async def action_reject(self) -> None:
        if self._is_typing():
            return
        await self._do_reject()

    async def action_edit_dirs(self) -> None:
        if self._is_typing():
            return
        sid = self.selected_session
        if not sid and len(self.agent.bindings) == 1:
            sid = next(iter(self.agent.bindings))
            self.selected_session = sid
        if not sid:
            return
        b = self.agent.bindings.get(sid)
        if b is None:
            return
        self.push_screen(EditDirsScreen(b.title, b.dirs), self._on_edited_dirs)

    def _on_edited_dirs(self, dirs: list[str] | None) -> None:
        if dirs is None or not self.selected_session:
            return
        sid = self.selected_session

        async def _apply() -> None:
            try:
                await self.agent.update_binding_dirs(sid, dirs)
            except PolicyError as exc:
                self.notify(str(exc), severity="error")

        asyncio.create_task(_apply())

    async def action_kill_cmd(self) -> None:
        if self._is_typing():
            return
        if self.selected_cmd_id:
            await self.agent.kill_command(self.selected_cmd_id)

    async def action_unbind_session(self) -> None:
        if self._is_typing():
            return
        if self.selected_session:
            await self.agent.unbind_session(self.selected_session)
            self.selected_session = None
            self._last_sessions_sig = None
            self._last_cmds_sig = None
            self._refresh_all()

    async def action_quit_node(self) -> None:
        await self.agent.stop()
        if self._agent_task:
            self._agent_task.cancel()
        self.exit()
