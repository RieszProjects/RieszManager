"""Multi-panel Textual TUI for `riesz-node` with interactive multi-directory selection."""

from __future__ import annotations

import asyncio
import os
import string
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, DirectoryTree, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static, Tree

from .agent import CommandEntry, NodeAgent
from .policy import PolicyError, normalize_roots, parse_dir_list

STATUS_ICON = {"running": "⏳", "confirming": "🔒", "done": "✓", "error": "✗", "killed": "⊘", "denied": "⛔"}
RISK_LABEL = {"low": "低", "medium": "中", "high": "高"}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _list_fs_roots() -> list[Path]:
    """Return available drive roots on Windows or `/` on POSIX."""
    if os.name == "nt":
        drives: list[Path] = []
        for ch in string.ascii_uppercase:
            p = Path(f"{ch}:\\")
            try:
                if p.exists():
                    drives.append(p)
            except OSError:
                continue
        return drives or [Path.home()]
    return [Path("/")]


class FolderOnlyTree(DirectoryTree):
    def filter_paths(self, paths):  # type: ignore[override]
        out = []
        for p in paths:
            try:
                if p.is_dir():
                    out.append(p)
            except OSError:
                continue
        return out


class DirPickerScreen(ModalScreen[tuple[str, Path] | None]):
    """Interactive folder tree modal with parent navigation, drive switching, and add/replace modes."""

    CSS = """
    DirPickerScreen { align: center middle; }
    #picker-box {
        width: 86%;
        max-width: 104;
        height: 84%;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #picker-title { text-style: bold; margin-bottom: 1; }
    #picker-nav { height: auto; margin-bottom: 1; }
    #picker-nav Button { margin-right: 1; min-width: 6; }
    #picker-current { color: $accent; margin-bottom: 1; }
    #picker-tree { height: 1fr; border: solid $panel; margin-bottom: 1; }
    #picker-actions { height: auto; }
    #picker-actions Button { margin-right: 1; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("backspace", "go_up", "上一级"),
    ]

    def __init__(self, start_path: Path, *, allow_replace: bool = True) -> None:
        super().__init__()
        self.tree_root: Path = start_path if start_path.is_dir() else Path.home()
        self.chosen: Path = self.tree_root
        self.allow_replace = allow_replace
        self._fs_roots = _list_fs_roots()

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static("浏览并选择要授权的本地目录（支持返回上一级 / 切换盘符）", id="picker-title")
            with Horizontal(id="picker-nav"):
                yield Button("⬆ 上一级", variant="default", id="pick-up")
                yield Button("🏠 主目录", variant="default", id="pick-home")
                for idx, root in enumerate(self._fs_roots):
                    yield Button(str(root), variant="default", id=f"pick-root-{idx}")
            yield Static(self._status_line(), id="picker-current")
            yield FolderOnlyTree(str(self.tree_root), id="picker-tree")
            with Horizontal(id="picker-actions"):
                yield Button("追加此目录（多目录）", variant="success", id="pick-add")
                if self.allow_replace:
                    yield Button("仅用此目录（替换）", variant="primary", id="pick-replace")
                yield Button("取消", variant="error", id="pick-cancel")

    def _status_line(self) -> str:
        return f"当前浏览：{self.tree_root}   |   已选中：{self.chosen}"

    def _set_root(self, new_root: Path) -> None:
        try:
            if not new_root.is_dir():
                return
        except OSError:
            return
        self.tree_root = new_root
        self.chosen = new_root
        tree = self.query_one("#picker-tree", FolderOnlyTree)
        tree.path = str(new_root)
        self.query_one("#picker-current", Static).update(self._status_line())

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.chosen = Path(event.path)
        self.query_one("#picker-current", Static).update(self._status_line())

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = getattr(event.node, "data", None)
        path = getattr(data, "path", None)
        if path is not None:
            self.chosen = Path(path)
            self.query_one("#picker-current", Static).update(self._status_line())

    def action_go_up(self) -> None:
        parent = self.tree_root.parent
        if parent != self.tree_root:
            self._set_root(parent)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "pick-add":
            self.dismiss(("add", self.chosen))
        elif bid == "pick-replace":
            self.dismiss(("replace", self.chosen))
        elif bid == "pick-up":
            self.action_go_up()
        elif bid == "pick-home":
            self._set_root(Path.home())
        elif bid.startswith("pick-root-"):
            try:
                idx = int(bid.split("-")[-1])
                self._set_root(self._fs_roots[idx])
            except (ValueError, IndexError):
                pass
        elif bid == "pick-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DirRow(ListItem):
    def __init__(self, index: int, path: Path) -> None:
        super().__init__(Label(f"{index + 1}. {path}"))
        self.dir_path = path


class EditDirsScreen(ModalScreen[list[str] | None]):
    """Interactive multi-directory manager modal for a bound session or default presets."""

    CSS = """
    EditDirsScreen { align: center middle; }
    #edit-box {
        width: 86%;
        max-width: 104;
        height: 82%;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #edit-title { text-style: bold; margin-bottom: 1; }
    #edit-subtitle { color: $text-muted; margin-bottom: 1; }
    #dir-list { height: 1fr; min-height: 6; border: solid $panel; margin-bottom: 1; }
    #edit-add-row { height: auto; margin-bottom: 1; }
    #edit-input { width: 1fr; margin-right: 1; }
    #edit-add-row Button { margin-right: 1; }
    #edit-error { color: $error; height: auto; margin-bottom: 1; }
    #edit-actions { height: auto; }
    #edit-actions Button { margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, title: str, current_dirs: list[Path], *, allow_empty: bool = False) -> None:
        super().__init__()
        self.modal_title = title
        self.allow_empty = allow_empty
        self.dirs: list[Path] = list(current_dirs)

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Static(self.modal_title, id="edit-title")
            yield Static("已授权目录列表（可添加多个目录，选中后可移除）：", id="edit-subtitle")
            yield ListView(id="dir-list")
            with Horizontal(id="edit-add-row"):
                yield Input(placeholder="粘贴或输入目录路径后按回车添加（多个可用 ; 分隔）…", id="edit-input")
                yield Button("+ 添加", variant="primary", id="edit-add")
                yield Button("浏览目录…", variant="default", id="edit-browse")
            yield Static("", id="edit-error")
            with Horizontal(id="edit-actions"):
                yield Button("保存生效", variant="success", id="edit-save")
                yield Button("移除选中", variant="warning", id="edit-remove")
                yield Button("取消", variant="error", id="edit-cancel")

    def on_mount(self) -> None:
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        lv = self.query_one("#dir-list", ListView)
        lv.clear()
        if not self.dirs:
            lv.append(ListItem(Label("（当前未配置目录）")))
            return
        for idx, p in enumerate(self.dirs):
            lv.append(DirRow(idx, p))

    def _set_error(self, msg: str) -> None:
        self.query_one("#edit-error", Static).update(msg)

    def _absorb_input(self) -> bool:
        inp = self.query_one("#edit-input", Input)
        raw = inp.value.strip()
        if not raw:
            return True
        parsed = parse_dir_list(raw)
        try:
            resolved = normalize_roots(parsed, allow_empty=False)
        except PolicyError as exc:
            self._set_error(str(exc))
            return False
        for p in resolved:
            if p not in self.dirs:
                self.dirs.append(p)
        inp.value = ""
        self._set_error("")
        self._rebuild_list()
        return True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "edit-input":
            self._absorb_input()

    def _save(self) -> None:
        if not self._absorb_input():
            return
        try:
            validated = normalize_roots([str(p) for p in self.dirs], allow_empty=self.allow_empty)
        except PolicyError as exc:
            self._set_error(str(exc))
            return
        self.dismiss([str(p) for p in validated])

    def _remove_selected(self) -> None:
        lv = self.query_one("#dir-list", ListView)
        item = lv.highlighted_child
        if isinstance(item, DirRow) and item.dir_path in self.dirs:
            self.dirs.remove(item.dir_path)
            self._set_error("")
            self._rebuild_list()
        elif self.dirs and len(self.dirs) == 1:
            self.dirs.pop()
            self._set_error("")
            self._rebuild_list()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "edit-add":
            self._absorb_input()
        elif bid == "edit-save":
            self._save()
        elif bid == "edit-remove":
            self._remove_selected()
        elif bid == "edit-browse":
            start = self.dirs[-1] if self.dirs else Path.cwd()
            self.app.push_screen(DirPickerScreen(start, allow_replace=True), self._on_picked)
        elif bid == "edit-cancel":
            self.dismiss(None)

    def _on_picked(self, result: tuple[str, Path] | None) -> None:
        if result is None:
            return
        action, path = result
        try:
            resolved = normalize_roots([str(path)], allow_empty=False)[0]
        except PolicyError as exc:
            self._set_error(str(exc))
            return
        if action == "replace":
            self.dirs = [resolved]
        elif resolved not in self.dirs:
            self.dirs.append(resolved)
        self._set_error("")
        self._rebuild_list()

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
    #bind-dir-row Button { margin-right: 1; }
    #prompt-error { color: $error; height: auto; display: none; margin-bottom: 1; }
    #prompt-error.visible { display: block; }
    #prompt-actions { height: auto; }
    #prompt-actions Button { margin-right: 1; }
    #main { height: 1fr; }
    #left-pane {
        width: 34%;
        min-width: 28;
        max-width: 50;
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
    #left-actions {
        height: auto;
        padding: 0 1;
        border-top: solid $primary-background;
        background: $surface;
    }
    #left-actions Button { margin: 1 1 1 0; min-width: 10; }
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
                yield Button("浏览 / 换盘…", variant="primary", id="btn-browse")
                yield Button("多目录管理…", variant="default", id="btn-manage-bind-dirs")
            yield Static("", id="prompt-error")
            with Horizontal(id="prompt-actions"):
                yield Button("同意 (y / Ctrl+Y)", variant="success", id="btn-allow")
                yield Button("拒绝 (n)", variant="error", id="btn-deny")
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                yield Static("已绑定会话 (选中后改目录/解绑)", classes="pane-title")
                yield ListView(id="sessions")
                with Horizontal(id="left-actions"):
                    yield Button("改目录 (e)", variant="primary", id="btn-edit-dirs")
                    yield Button("解绑 (u)", variant="warning", id="btn-unbind")
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
        preset = "; ".join(str(r) for r in self.agent.roots) if self.agent.roots else "按会话单独授权（按 e 可设默认目录）"
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
                f"请指定允许该会话操作的本地目录（支持多个目录用 ; 分隔，不同会话目录互相隔离）："
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
                log_w.write(
                    "暂无执行记录。\n"
                    "• 点击左侧「改目录 (e)」可预设默认允许目录，或为已绑定会话单独添加/移除多个目录。\n"
                    "• 在 Riesz Manager 网页中将本节点绑定到某个会话并在此授权目录后，指令输出会实时显示在这里。"
                )
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

    def _select_session_item(self, item: ListItem | None) -> None:
        if isinstance(item, SessionRow) and item.session_id != self.selected_session:
            self.selected_session = item.session_id
            self.selected_cmd_id = None
            self._last_cmds_sig = None
            self._refresh_all()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "sessions":
            self._select_session_item(event.item)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "sessions":
            self._select_session_item(event.item)

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
        bid = event.button.id or ""
        if bid == "btn-allow":
            await self.action_approve_force()
        elif bid == "btn-deny":
            await self._do_reject()
        elif bid == "btn-browse":
            inp = self.query_one("#bind-dirs-input", Input)
            existing = parse_dir_list(inp.value)
            start = Path(os.path.expanduser(existing[-1])) if existing else Path.cwd()
            self.push_screen(DirPickerScreen(start, allow_replace=True), self._on_bind_dir_picked)
        elif bid == "btn-manage-bind-dirs":
            inp = self.query_one("#bind-dirs-input", Input)
            cur = [Path(os.path.expanduser(p)) for p in parse_dir_list(inp.value)]
            title = "管理本会话将授权的目录（可添加多个目录）"
            self.push_screen(EditDirsScreen(title, cur, allow_empty=False), self._on_bind_dirs_managed)
        elif bid == "btn-edit-dirs":
            await self.action_edit_dirs()
        elif bid == "btn-unbind":
            await self.action_unbind_session()

    def _on_bind_dir_picked(self, result: tuple[str, Path] | None) -> None:
        if result is None:
            return
        action, path = result
        inp = self.query_one("#bind-dirs-input", Input)
        s = str(path)
        if action == "replace":
            inp.value = s
        else:
            existing = parse_dir_list(inp.value)
            if s not in existing:
                existing.append(s)
            inp.value = "; ".join(existing)
        self._set_prompt_error("")

    def _on_bind_dirs_managed(self, dirs: list[str] | None) -> None:
        if dirs is None:
            return
        self.query_one("#bind-dirs-input", Input).value = "; ".join(dirs)
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
        if sid and sid in self.agent.bindings:
            b = self.agent.bindings[sid]
            title = f"编辑会话「{b.title}」的允许目录（支持多个目录）"
            self.push_screen(
                EditDirsScreen(title, b.dirs, allow_empty=False),
                lambda dirs, _sid=sid: self._on_edited_session_dirs(_sid, dirs),
            )
            return
        # No specific session selected (0 bindings, or "全部会话" selected with multiple bindings): edit default preset dirs
        title = "编辑默认预填目录（新会话绑定时自动预填，也可在绑定时按会话单独调整）"
        self.push_screen(EditDirsScreen(title, self.agent.roots, allow_empty=True), self._on_edited_default_dirs)

    def _on_edited_session_dirs(self, sid: str, dirs: list[str] | None) -> None:
        if dirs is None:
            return

        async def _apply() -> None:
            try:
                await self.agent.update_binding_dirs(sid, dirs)
                self.notify("已更新该会话的允许目录", severity="information")
            except PolicyError as exc:
                self.notify(str(exc), severity="error")

        asyncio.create_task(_apply())

    def _on_edited_default_dirs(self, dirs: list[str] | None) -> None:
        if dirs is None:
            return

        async def _apply() -> None:
            try:
                await self.agent.update_default_dirs(dirs)
                self.notify("已更新默认预填目录", severity="information")
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
        sid = self.selected_session
        if not sid and len(self.agent.bindings) == 1:
            sid = next(iter(self.agent.bindings))
        if sid:
            await self.agent.unbind_session(sid)
            self.selected_session = None
            self._last_sessions_sig = None
            self._last_cmds_sig = None
            self._refresh_all()

    async def action_quit_node(self) -> None:
        await self.agent.stop()
        if self._agent_task:
            self._agent_task.cancel()
        self.exit()
