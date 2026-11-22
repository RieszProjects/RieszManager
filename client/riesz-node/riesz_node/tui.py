"""Single-session Textual TUI for `riesz-node` with multi-directory manager & live rename."""

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
MODE_LABEL = {"readonly": "只读", "ask": "逐项确认", "auto": "智能风控", "full": "目录全放行"}
MODE_CHOICES = [
    ("readonly", "只读 — 仅允许白名单目录内只读命令与拉取文件"),
    ("ask", "逐项确认 — 每条命令与文件推送都需人工确认"),
    ("auto", "智能风控（推荐） — 本地硬规则 + 模型风控，高危需确认"),
    ("full", "目录全放行 — 白名单目录内全自动执行（本地高危硬拦截仍生效）"),
]


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
    """Interactive multi-directory manager modal for the bound session."""

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

    def __init__(self, title: str, current_dirs: list[Path]) -> None:
        super().__init__()
        self.modal_title = title
        self.dirs: list[Path] = list(current_dirs)

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Static(self.modal_title, id="edit-title")
            yield Static("已授权目录列表（可添加多个目录，下次再绑定该会话会自动恢复）：", id="edit-subtitle")
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
            lv.append(ListItem(Label("（尚未添加目录，请从下方输入或点击「浏览目录…」）")))
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
            validated = normalize_roots([str(p) for p in self.dirs], allow_empty=False)
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
        elif len(self.dirs) == 1:
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


class RenameScreen(ModalScreen[str | None]):
    """Modal to rename this client process (persisted as convenience memory for this session)."""

    CSS = """
    RenameScreen { align: center middle; }
    #rename-box {
        width: 68%;
        max-width: 72;
        height: auto;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #rename-title { text-style: bold; margin-bottom: 1; }
    #rename-input { margin-bottom: 1; }
    #rename-error { color: $error; height: auto; margin-bottom: 1; }
    #rename-actions { height: auto; }
    #rename-actions Button { margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, current_name: str) -> None:
        super().__init__()
        self.current_name = current_name

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-box"):
            yield Static("修改本客户端显示名称（下次再绑定本会话会自动沿用，重名时自动加后缀）", id="rename-title")
            yield Input(value=self.current_name, placeholder="输入名称…", id="rename-input")
            yield Static("", id="rename-error")
            with Horizontal(id="rename-actions"):
                yield Button("确定", variant="success", id="rename-ok")
                yield Button("取消", variant="error", id="rename-cancel")

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    def _submit(self) -> None:
        val = " ".join(self.query_one("#rename-input", Input).value.strip().split())[:48]
        if not val:
            self.query_one("#rename-error", Static).update("名称不能为空")
            return
        self.dismiss(val)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "rename-input":
            self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rename-ok":
            self._submit()
        elif event.button.id == "rename-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModeScreen(ModalScreen[str | None]):
    """Modal to switch permission mode on the client (cached per session)."""

    CSS = """
    ModeScreen { align: center middle; }
    #mode-box {
        width: 78%;
        max-width: 86;
        height: auto;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #mode-title { text-style: bold; margin-bottom: 1; }
    #mode-box Button { width: 100%; margin-bottom: 1; content-align: left middle; }
    #mode-cancel { width: auto; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, current_mode: str) -> None:
        super().__init__()
        self.current_mode = current_mode

    def compose(self) -> ComposeResult:
        with Vertical(id="mode-box"):
            yield Static("选择本会话的权限模式（由电脑端独立控制，下次再绑定本会话自动恢复）", id="mode-title")
            for key, desc in MODE_CHOICES:
                mark = "● " if key == self.current_mode else "○ "
                yield Button(f"{mark}{desc}", variant="primary" if key == self.current_mode else "default", id=f"mode-{key}")
            yield Button("取消 (Esc)", variant="error", id="mode-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "mode-cancel":
            self.dismiss(None)
        elif bid.startswith("mode-"):
            self.dismiss(bid.removeprefix("mode-"))

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    #dirs-view { height: 1fr; }
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
        Binding("n", "reject", "拒绝", show=True),
        Binding("e", "edit_dirs", "改目录", show=True),
        Binding("m", "change_mode", "切权限", show=True),
        Binding("r", "rename_node", "改名称", show=True),
        Binding("k", "kill_cmd", "终止命令", show=True),
        Binding("ctrl+q", "quit_node", "断开并退出", show=True),
    ]

    def __init__(self, agent: NodeAgent, *, auto_connect: bool = True, prompt_dirs_if_empty: bool = True) -> None:
        super().__init__()
        self.agent = agent
        self.auto_connect = auto_connect
        self.prompt_dirs_if_empty = prompt_dirs_if_empty
        self.selected_cmd_id: str | None = None
        self._agent_task: asyncio.Task[None] | None = None
        self._refresh_pending = False
        self._last_dirs_sig: tuple[str, ...] | None = None
        self._last_cmds_sig: tuple | None = None
        self._rendered_cmd_id: str | None = None
        self._rendered_header_sig: tuple | None = None
        self._rendered_out_len: int = 0
        self._prompted_initial_dirs = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status-bar")
        with Vertical(id="prompt-bar"):
            yield Static("", id="prompt-text")
            with Horizontal(id="prompt-actions"):
                yield Button("同意 (y)", variant="success", id="btn-allow")
                yield Button("拒绝 (n)", variant="error", id="btn-deny")
                yield Button("选择目录 (e)", variant="primary", id="btn-pick-dirs")
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                yield Static("本会话允许目录（严格隔离）", classes="pane-title")
                yield ListView(id="dirs-view")
                with Horizontal(id="left-actions"):
                    yield Button("改目录 (e)", variant="primary", id="btn-edit-dirs")
                    yield Button("切权限 (m)", variant="default", id="btn-mode")
                    yield Button("改名称 (r)", variant="default", id="btn-rename")
            with Vertical(id="right-pane"):
                yield Static("指令与文件传输记录 (k 终止运行中命令)", classes="pane-title")
                yield DataTable(id="commands", cursor_type="row")
                yield Static("实时输出详情", classes="pane-title")
                yield RichLog(id="output", highlight=False, markup=False, wrap=True)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#commands", DataTable)
        table.add_columns("时间", "状态", "风险", "命令 / 传输", "退出码")
        self.agent.on_update = self._schedule_refresh
        self._refresh_all()
        self.query_one("#dirs-view", ListView).focus()
        if self.auto_connect:
            self._agent_task = asyncio.create_task(self.agent.run())
        if self.prompt_dirs_if_empty and not self.agent.dirs and not self._prompted_initial_dirs:
            self._prompted_initial_dirs = True
            self.set_timer(0.15, self._open_initial_dirs_if_empty)

    def _open_initial_dirs_if_empty(self) -> None:
        if not self.agent.dirs and len(self.screen_stack) == 1:
            self._open_edit_dirs()

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

    def _refresh_all(self) -> None:
        # 1. Status bar
        dot = {"online": "🟢 在线", "connecting": "🟡 连接中", "kicked": "🔴 已断开"}.get(self.agent.status, "⚪ 离线")
        harness_info = f"{self.agent.harness} · " if self.agent.harness else ""
        mode_txt = MODE_LABEL.get(self.agent.mode, self.agent.mode)
        self.query_one("#status-bar", Static).update(
            f"{dot}  |  本机名称: {self.agent.name}  |  绑定会话: 「{self.agent.session_title}」({harness_info}{self.agent.session_id[:8]})  |  权限: {mode_txt}\n"
            f"服务器: {self.agent.server}  —  {self.agent.status_detail}"
        )

        # 2. Prompt bar (local high-risk confirm > normal approvals > empty-dirs reminder)
        bar = self.query_one("#prompt-bar", Vertical)
        text_w = self.query_one("#prompt-text", Static)
        btn_allow = self.query_one("#btn-allow", Button)
        btn_deny = self.query_one("#btn-deny", Button)
        btn_pick = self.query_one("#btn-pick-dirs", Button)
        bar.remove_class("danger")

        if self.agent.pending_local_confirms:
            lc = self.agent.pending_local_confirms[0]
            text_w.update(
                f"【本机安全拦截 · 必须在本机确认】\n"
                f"命令：{lc.command}\n原因：{lc.reason}"
            )
            btn_allow.display = True
            btn_deny.display = True
            btn_pick.display = False
            bar.add_class("visible")
            bar.add_class("danger")
        elif self.agent.pending_approvals:
            a = self.agent.pending_approvals[0]
            text_w.update(
                f"【指令审批 · 风险:{RISK_LABEL.get(a.risk, a.risk)}】\n"
                f"命令：{a.command}\n原因：{a.reason}"
            )
            btn_allow.display = True
            btn_deny.display = True
            btn_pick.display = False
            bar.add_class("visible")
        elif not self.agent.dirs:
            text_w.update(
                f"【首次绑定提示】尚未为会话「{self.agent.session_title}」授权本地目录，目前所有命令与文件读写都会被拦截。\n"
                f"请按 e 或点击下方按钮添加至少一个允许目录（可添加多个，下次再绑定本会话会自动恢复）："
            )
            btn_allow.display = False
            btn_deny.display = False
            btn_pick.display = True
            bar.add_class("visible")
        else:
            bar.remove_class("visible")

        # 3. Allowed directories list
        dirs_sig = tuple(str(d) for d in self.agent.dirs)
        if dirs_sig != self._last_dirs_sig:
            self._last_dirs_sig = dirs_sig
            lv = self.query_one("#dirs-view", ListView)
            lv.clear()
            if not self.agent.dirs:
                lv.append(ListItem(Label("（尚未授权目录，按 e 添加）")))
            else:
                for idx, d in enumerate(self.agent.dirs):
                    lv.append(DirRow(idx, d))

        # 4. Commands table
        cmds_sig = tuple((c.cmd_id, c.status, c.exit_code, c.risk) for c in self.agent.commands)
        if cmds_sig != self._last_cmds_sig:
            self._last_cmds_sig = cmds_sig
            table = self.query_one("#commands", DataTable)
            table.clear()
            target_row = 0
            for idx, c in enumerate(self.agent.commands):
                icon = STATUS_ICON.get(c.status, "•")
                code = "" if c.exit_code is None else str(c.exit_code)
                table.add_row(
                    _fmt_time(c.started_at), icon,
                    RISK_LABEL.get(c.risk, c.risk), c.command[:78], code,
                    key=c.cmd_id,
                )
                if c.cmd_id == self.selected_cmd_id:
                    target_row = idx
            if self.agent.commands:
                table.move_cursor(row=target_row, animate=False)

        # 5. Selected command output — incremental append
        self._render_output()

    def _render_output(self) -> None:
        log_w = self.query_one("#output", RichLog)
        entry = None
        if self.selected_cmd_id:
            entry = next((c for c in self.agent.commands if c.cmd_id == self.selected_cmd_id), None)
        if entry is None and self.agent.commands:
            entry = self.agent.commands[0]
            self.selected_cmd_id = entry.cmd_id
        if entry is None:
            if self._rendered_cmd_id != "__empty__":
                self._rendered_cmd_id = "__empty__"
                self._rendered_header_sig = None
                self._rendered_out_len = 0
                log_w.clear()
                log_w.write(
                    f"已绑定会话：「{self.agent.session_title}」({self.agent.session_id[:8]})，仅该会话可见并操作本客户端。\n"
                    "• 按 e（或点「改目录」）随时添加/移除多个允许目录；\n"
                    "• 按 m（或点「切权限」）随时切换权限模式（只读 / 逐项确认 / 智能风控 / 目录全放行）；\n"
                    "• 按 r（或点「改名称」）修改本客户端名称；\n"
                    "• 按 Ctrl+Q 立即断开连接并销毁本次临时密钥。"
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
        log_w.write(f"状态：{entry.status}  |  目录：{entry.cwd}")
        log_w.write(f"命令：{entry.command}")
        if entry.risk_reason:
            log_w.write(f"风控（{RISK_LABEL.get(entry.risk, entry.risk)}）：{entry.risk_reason}")
        log_w.write("─" * 60)
        log_w.write(entry.output or "（尚无输出）")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.selected_cmd_id = str(event.row_key.value)
            self._render_output()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self.selected_cmd_id = str(event.row_key.value)
            self._render_output()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-allow":
            await self.action_approve()
        elif bid == "btn-deny":
            await self.action_reject()
        elif bid in ("btn-edit-dirs", "btn-pick-dirs"):
            self._open_edit_dirs()
        elif bid == "btn-mode":
            self.action_change_mode()
        elif bid == "btn-rename":
            self.action_rename_node()

    async def action_approve(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self.agent.pending_local_confirms:
            await self.agent.respond_local_confirm(self.agent.pending_local_confirms[0], allow=True)
        elif self.agent.pending_approvals:
            await self.agent.respond_approval(self.agent.pending_approvals[0], allow=True)

    async def action_reject(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self.agent.pending_local_confirms:
            await self.agent.respond_local_confirm(self.agent.pending_local_confirms[0], allow=False)
        elif self.agent.pending_approvals:
            await self.agent.respond_approval(self.agent.pending_approvals[0], allow=False)

    def _open_edit_dirs(self) -> None:
        title = f"管理会话「{self.agent.session_title}」的允许目录（支持多目录）"
        self.push_screen(EditDirsScreen(title, self.agent.dirs), self._on_edited_dirs)

    def action_edit_dirs(self) -> None:
        if isinstance(self.focused, Input):
            return
        self._open_edit_dirs()

    def _on_edited_dirs(self, dirs: list[str] | None) -> None:
        if dirs is None:
            return

        async def _apply() -> None:
            try:
                await self.agent.update_dirs(dirs)
                self.notify(f"已更新允许目录（共 {len(dirs)} 个）", severity="information")
            except PolicyError as exc:
                self.notify(str(exc), severity="error")

        asyncio.create_task(_apply())

    def action_change_mode(self) -> None:
        if isinstance(self.focused, Input):
            return
        self.push_screen(ModeScreen(self.agent.mode), self._on_mode_changed)

    def _on_mode_changed(self, new_mode: str | None) -> None:
        if not new_mode or new_mode == self.agent.mode:
            return

        async def _apply() -> None:
            try:
                await self.agent.set_mode(new_mode)
                self.notify(f"权限模式已切换为：{MODE_LABEL.get(new_mode, new_mode)}", severity="information")
            except PolicyError as exc:
                self.notify(str(exc), severity="error")

        asyncio.create_task(_apply())

    def action_rename_node(self) -> None:
        if isinstance(self.focused, Input):
            return
        self.push_screen(RenameScreen(self.agent.name), self._on_renamed)

    def _on_renamed(self, new_name: str | None) -> None:
        if not new_name:
            return

        async def _apply() -> None:
            try:
                await self.agent.rename(new_name)
                self.notify(f"已将本客户端改名为：{new_name}", severity="information")
            except PolicyError as exc:
                self.notify(str(exc), severity="error")

        asyncio.create_task(_apply())

    async def action_kill_cmd(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self.selected_cmd_id:
            await self.agent.kill_command(self.selected_cmd_id)

    async def action_quit_node(self) -> None:
        await self.agent.stop()
        if self._agent_task:
            self._agent_task.cancel()
        self.exit()
