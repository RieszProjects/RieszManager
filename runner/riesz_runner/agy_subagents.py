"""Built-in Antigravity subagents (`invoke_subagent`).

Antigravity runs `invoke_subagent` subagents inside the parent `agy` process and writes each
conversation to:

    ~/.gemini/antigravity-cli/brain/<conversationId>/.system_generated/logs/transcript.jsonl

The parent session's own `transcript.jsonl` records every `invoke_subagent` call (`Role`,
`TypeName`, `Prompt`) and its JSON result (`conversationId`, `logAbsoluteUri`), as well as
`manage_subagents` list/kill calls. We scan the parent's transcript incrementally (by file
offset/inode) to discover its built-in subagents, inspect each subagent's `transcript.jsonl`
for live running state, and convert its steps into Riesz Manager display items for SubagentViewer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .drivers.base import assistant_item, notice_item, tool_block, tool_result, user_item

DEFAULT_BRAIN_DIR = Path("~/.gemini/antigravity-cli/brain")
AGENT_PREFIX = "agy-"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CREATED_RE = re.compile(r"Created the following subagent:\s*(\{[\s\S]*?\})")
ACTIVE_LIST_RE = re.compile(r"You have \d+ active subagent\(s\):\s*(\[[\s\S]*?\])")
TIMESTAMPS_RE = re.compile(r"^(?:(?:Created|Completed) At:[^\n]*\n)+")
BG_TASK_START_RE = re.compile(r"Tool is running as a background task with task id:\s*([^\s]+)")
BG_TASK_DONE_RE = re.compile(r'Task id "([^"]+)" finished')
SYS_MSG_RE = re.compile(
    r"\[Message\]\s+timestamp=[^\s]+\s+sender=([^\s]+)\s+priority=[^\s]+\s+content=([\s\S]*?)(?:\n</SYSTEM_MESSAGE>|$)"
)
STALE_RUNNING_SECONDS = 900.0
VIEW_CACHE_SIZE = 8
MAX_TOOL_OUTPUT = 16_000


def _unwrap_arg(value: Any) -> Any:
    """Antigravity logs tool_call args as JSON-encoded strings inside the args dict."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if (len(s) >= 2 and s[0] == '"' and s[-1] == '"') or s in ("true", "false", "null") or (s and s[0] in "[{"):
        try:
            return json.loads(s)
        except ValueError:
            return value
    return value


def _unwrap_args(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): _unwrap_arg(v) for k, v in raw.items()}


def _clean_generic_content(text: str) -> str:
    return TIMESTAMPS_RE.sub("", text).strip()


def _file_uri_to_path(uri: str | None) -> Path | None:
    if not uri or not isinstance(uri, str):
        return None
    if uri.startswith("file://"):
        parsed = urllib.parse.urlparse(uri)
        return Path(urllib.parse.unquote(parsed.path))
    if uri.startswith("/"):
        return Path(uri)
    return None


def _normalize_tool(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map Antigravity built-in tool names/args to Riesz Manager's canonical ToolBlock shapes."""
    if name == "run_command":
        return "Bash", {"command": args.get("CommandLine") or "", "description": args.get("toolSummary") or ""}
    if name == "read_file":
        out: dict[str, Any] = {"file_path": args.get("Path") or ""}
        if args.get("StartLine") is not None:
            out["offset"] = args["StartLine"]
        if args.get("EndLine") is not None:
            out["limit"] = args["EndLine"]
        return "Read", out
    if name == "write_file":
        return "Write", {"file_path": args.get("Path") or "", "content": args.get("Content") or ""}
    if name == "edit_file":
        return "Edit", {
            "file_path": args.get("Path") or "",
            "old_string": args.get("Target") or "",
            "new_string": args.get("Replacement") or "",
        }
    if name == "search_web":
        return "WebSearch", {"query": args.get("query") or ""}
    if name == "send_message":
        return "SendMessage", {"to": args.get("Recipient") or "", "message": args.get("Message") or ""}
    if name == "invoke_subagent":
        return "Agent", {
            "description": args.get("Role") or "",
            "subagent_type": args.get("TypeName") or "subagent",
            "prompt": args.get("Prompt") or "",
        }
    return name, args


@dataclass
class _SubagentMeta:
    conversation_id: str
    role: str = ""
    agent_type: str = ""
    prompt: str = ""
    transcript_path: Path | None = None
    created_at: float = 0.0
    killed: bool = False


@dataclass
class _ParentIndex:
    inode: int = 0
    offset: int = 0
    pending_invokes: list[dict[str, str]] = field(default_factory=list)
    children: dict[str, _SubagentMeta] = field(default_factory=dict)


@dataclass
class _SubagentStatus:
    mtime: float = 0.0
    size: int = 0
    inode: int = 0
    offset: int = 0
    last_type: str = ""
    last_status: str = ""
    last_has_tools: bool = False
    open_bg_tasks: set[str] = field(default_factory=set)


@dataclass
class _SubagentView:
    inode: int = 0
    offset: int = 0
    seq: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    pending_tools: list[dict[str, Any]] = field(default_factory=list)


def _read_lines_from(path: Path, offset: int) -> tuple[list[str], int]:
    lines: list[str] = []
    with path.open("rb") as fh:
        fh.seek(offset)
        while True:
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break                               # partial line still being written
            offset = fh.tell()
            lines.append(raw.decode("utf-8", errors="replace"))
    return lines, offset


class AgySubagentStore:
    """Discovers and reads Antigravity built-in `invoke_subagent` transcripts."""

    def __init__(self, brain_dir: Path | None = None) -> None:
        self.brain_dir = Path(os.path.expanduser(str(brain_dir or DEFAULT_BRAIN_DIR)))
        self._parents: dict[str, _ParentIndex] = {}
        self._statuses: dict[str, _SubagentStatus] = {}
        self._views: OrderedDict[str, _SubagentView] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def _transcript_for(self, cid: str) -> Path:
        return self.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"

    def _safe_transcript(self, meta: _SubagentMeta) -> Path | None:
        candidate = meta.transcript_path or self._transcript_for(meta.conversation_id)
        try:
            real_root = Path(os.path.realpath(self.brain_dir))
            real_path = Path(os.path.realpath(candidate))
            if real_root not in real_path.parents:
                return None
            return real_path
        except OSError:
            return None

    def _sync_parent(self, parent_sid: str) -> _ParentIndex | None:
        if not UUID_RE.match(parent_sid):
            return None
        path = self._transcript_for(parent_sid)
        try:
            st = path.stat()
        except OSError:
            return self._parents.get(parent_sid)
        idx = self._parents.get(parent_sid)
        if idx is None or st.st_ino != idx.inode or st.st_size < idx.offset:
            idx = _ParentIndex(inode=st.st_ino, offset=0)
            self._parents[parent_sid] = idx
        if st.st_size == idx.offset:
            return idx
        try:
            lines, idx.offset = _read_lines_from(path, idx.offset)
        except OSError:
            return idx
        for raw in lines:
            try:
                step = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(step, dict):
                continue
            self._feed_parent_step(idx, step)
        return idx

    def _feed_parent_step(self, idx: _ParentIndex, step: dict[str, Any]) -> None:
        stype = step.get("type")
        if stype == "PLANNER_RESPONSE":
            for tc in step.get("tool_calls") or ():
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name")
                args = _unwrap_args(tc.get("args"))
                if name == "invoke_subagent":
                    idx.pending_invokes.append({
                        "role": str(args.get("Role") or "").strip(),
                        "type": str(args.get("TypeName") or "").strip(),
                        "prompt": str(args.get("Prompt") or "").strip(),
                    })
                elif name == "manage_subagents":
                    action = str(args.get("Action") or "").strip()
                    if action == "kill_all":
                        for child in idx.children.values():
                            child.killed = True
                    elif action == "kill":
                        ids = args.get("ConversationIds")
                        if isinstance(ids, list):
                            for cid in ids:
                                if isinstance(cid, str) and cid in idx.children:
                                    idx.children[cid].killed = True
        elif stype == "GENERIC":
            content = str(step.get("content") or "")
            if not content:
                return
            for m in CREATED_RE.finditer(content):
                try:
                    info = json.loads(m.group(1))
                except ValueError:
                    continue
                cid = info.get("conversationId") if isinstance(info, dict) else None
                if not isinstance(cid, str) or not UUID_RE.match(cid):
                    continue
                pending = idx.pending_invokes.pop(0) if idx.pending_invokes else {}
                meta = idx.children.get(cid)
                if meta is None:
                    meta = _SubagentMeta(conversation_id=cid, created_at=time.time())
                    idx.children[cid] = meta
                meta.role = meta.role or pending.get("role") or ""
                meta.agent_type = meta.agent_type or pending.get("type") or ""
                meta.prompt = meta.prompt or pending.get("prompt") or ""
                meta.transcript_path = _file_uri_to_path(info.get("logAbsoluteUri")) or meta.transcript_path
                meta.killed = False
            for m in ACTIVE_LIST_RE.finditer(content):
                try:
                    arr = json.loads(m.group(1))
                except ValueError:
                    continue
                if not isinstance(arr, list):
                    continue
                for row in arr:
                    if not isinstance(row, dict):
                        continue
                    cid = row.get("conversationId")
                    if not isinstance(cid, str) or not UUID_RE.match(cid):
                        continue
                    meta = idx.children.get(cid)
                    if meta is None:
                        meta = _SubagentMeta(conversation_id=cid, created_at=time.time())
                        idx.children[cid] = meta
                    if row.get("role"):
                        meta.role = str(row["role"])
                    if row.get("type"):
                        meta.agent_type = str(row["type"])
                    if row.get("transcript"):
                        meta.transcript_path = _file_uri_to_path(str(row["transcript"])) or meta.transcript_path

    def _sync_status(self, meta: _SubagentMeta) -> _SubagentStatus:
        path = self._safe_transcript(meta)
        status = self._statuses.get(meta.conversation_id)
        if path is None:
            return status or _SubagentStatus()
        try:
            st = path.stat()
        except OSError:
            return status or _SubagentStatus()
        if status is not None and status.inode == st.st_ino and status.size == st.st_size and status.mtime == st.st_mtime:
            return status
        if status is None or st.st_ino != status.inode or st.st_size < status.offset:
            status = _SubagentStatus(inode=st.st_ino, offset=0)
            self._statuses[meta.conversation_id] = status
        status.mtime = st.st_mtime
        status.size = st.st_size
        try:
            lines, status.offset = _read_lines_from(path, status.offset)
        except OSError:
            return status
        for raw in lines:
            try:
                step = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(step, dict):
                continue
            stype = str(step.get("type") or "")
            sstat = str(step.get("status") or "")
            status.last_type = stype
            status.last_status = sstat
            if stype == "PLANNER_RESPONSE":
                status.last_has_tools = bool(step.get("tool_calls"))
            elif stype == "GENERIC":
                content = str(step.get("content") or "")
                m = BG_TASK_START_RE.search(content)
                if m:
                    status.open_bg_tasks.add(m.group(1))
            elif stype == "SYSTEM_MESSAGE":
                content = str(step.get("content") or "")
                for m in BG_TASK_DONE_RE.finditer(content):
                    status.open_bg_tasks.discard(m.group(1))
                if "All your subagents and background tasks have been stopped" in content:
                    status.open_bg_tasks.clear()
        return status

    def _is_running(self, meta: _SubagentMeta, status: _SubagentStatus, *, parent_alive: bool, now: float) -> bool:
        if not parent_alive or meta.killed:
            return False
        if status.size == 0:
            return meta.created_at > 0 and (now - meta.created_at) < 60.0
        if status.last_status == "RUNNING" or status.open_bg_tasks:
            return (now - status.mtime) < STALE_RUNNING_SECONDS
        if status.last_type == "PLANNER_RESPONSE" and status.last_status == "DONE" and not status.last_has_tools:
            return False
        return (now - status.mtime) < STALE_RUNNING_SECONDS

    def list(self, parent_sid: str, *, parent_alive: bool) -> list[dict[str, Any]]:
        idx = self._sync_parent(parent_sid)
        if idx is None or not idx.children:
            return []
        now = time.time()
        out: list[dict[str, Any]] = []
        for meta in idx.children.values():
            status = self._sync_status(meta)
            running = self._is_running(meta, status, parent_alive=parent_alive, now=now)
            desc = meta.role or (meta.prompt.splitlines()[0][:120] if meta.prompt else meta.conversation_id[:8])
            out.append({
                "agentId": AGENT_PREFIX + meta.conversation_id,
                "toolUseId": None,
                "description": desc,
                "agentType": meta.agent_type or "subagent",
                "harness": "antigravity",
                "background": True,
                "finished": not running,
                "running": running,
                "size": status.size,
                "mtime": status.mtime or meta.created_at,
            })
        out.sort(key=lambda a: a["mtime"])
        return out

    def running_ids(self, parent_sid: str, *, parent_alive: bool) -> list[str]:
        return [a["agentId"] for a in self.list(parent_sid, parent_alive=parent_alive) if a["running"]]

    def has_running(self, parent_sid: str, *, parent_alive: bool) -> bool:
        return bool(self.running_ids(parent_sid, parent_alive=parent_alive))

    async def items(self, parent_sid: str, agent_id: str, *, parent_alive: bool) -> tuple[list[dict[str, Any]], float, bool] | None:
        """All display items of one Antigravity built-in subagent, plus `(mtime, running)`."""
        if not agent_id.startswith(AGENT_PREFIX):
            return None
        cid = agent_id[len(AGENT_PREFIX):]
        if not UUID_RE.match(cid):
            return None
        idx = self._sync_parent(parent_sid)
        if idx is None or cid not in idx.children:
            return None
        meta = idx.children[cid]
        path = self._safe_transcript(meta)
        if path is None or not path.is_file():
            return [], meta.created_at, self._is_running(meta, _SubagentStatus(), parent_alive=parent_alive, now=time.time())
        key = str(path)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            items, mtime = await asyncio.to_thread(self._catch_up, key, path, parent_sid)
        status = self._sync_status(meta)
        running = self._is_running(meta, status, parent_alive=parent_alive, now=time.time())
        return items, mtime, running

    def _catch_up(self, key: str, path: Path, parent_sid: str) -> tuple[list[dict[str, Any]], float]:
        st = path.stat()
        view = self._views.get(key)
        if view is None or st.st_ino != view.inode or st.st_size < view.offset:
            view = _SubagentView(inode=st.st_ino, offset=0)
            self._views[key] = view
        self._views.move_to_end(key)
        while len(self._views) > VIEW_CACHE_SIZE:
            old, _ = self._views.popitem(last=False)
            self._locks.pop(old, None)
        if st.st_size > view.offset:
            lines, view.offset = _read_lines_from(path, view.offset)
            for raw in lines:
                try:
                    step = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(step, dict):
                    self._feed_view_step(view, step, parent_sid)
        return list(view.items), st.st_mtime

    def _push_item(self, view: _SubagentView, item: dict[str, Any]) -> None:
        view.seq += 1
        item["seq"] = view.seq
        view.items.append(item)

    def _feed_view_step(self, view: _SubagentView, step: dict[str, Any], parent_sid: str) -> None:
        stype = step.get("type")
        ts = step.get("created_at")
        idx = step.get("step_index", view.seq)

        if stype == "USER_INPUT":
            text = str(step.get("content") or "").strip()
            if text:
                self._push_item(view, user_item(f"agy:u:{idx}", text, ts=ts, origin="user"))
            return

        if stype == "SYSTEM_MESSAGE":
            content = str(step.get("content") or "")
            if "Reminder: work efficiently" in content:
                return
            m = SYS_MSG_RE.search(content)
            if m:
                sender, msg_text = m.group(1), m.group(2).strip()
                if BG_TASK_DONE_RE.search(msg_text) or msg_text.startswith("[Notice]"):
                    return
                if msg_text:
                    origin = "user" if sender == parent_sid else "peer"
                    self._push_item(view, user_item(f"agy:m:{idx}", msg_text, ts=ts, origin=origin))
            return

        if stype == "CHECKPOINT":
            self._push_item(view, notice_item(f"agy:cp:{idx}", "上下文已压缩", level="info"))
            return

        if stype == "PLANNER_RESPONSE":
            blocks: list[dict[str, Any]] = []
            thinking = str(step.get("thinking") or "").strip()
            if thinking:
                blocks.append({"type": "thinking", "text": thinking, "redacted": False})
            text = str(step.get("content") or "").strip()
            if text:
                blocks.append({"type": "text", "text": text})
            new_tools: list[dict[str, Any]] = []
            for ti, tc in enumerate(step.get("tool_calls") or ()):
                if not isinstance(tc, dict):
                    continue
                tname, targs = _normalize_tool(str(tc.get("name") or "Tool"), _unwrap_args(tc.get("args")))
                blk = tool_block(f"agy:t:{idx}:{ti}", tname, targs)
                blocks.append(blk)
                new_tools.append(blk)
            if blocks:
                self._push_item(view, assistant_item(f"agy:a:{idx}", blocks, ts=ts, model=step.get("model")))
            view.pending_tools.extend(new_tools)
            return

        if stype == "GENERIC":
            if not view.pending_tools:
                return
            blk = view.pending_tools.pop(0)
            status = str(step.get("status") or "")
            err = step.get("error")
            cleaned = _clean_generic_content(str(step.get("content") or ""))
            if err:
                cleaned = f"{err}\n{cleaned}".strip() if cleaned else str(err)
            if status == "RUNNING" and not BG_TASK_START_RE.search(cleaned):
                # Still executing right now: leave result unset so SubagentViewer shows spinner.
                return
            blk["result"] = tool_result(cleaned[:MAX_TOOL_OUTPUT], error=(status == "ERROR" or bool(err)))
