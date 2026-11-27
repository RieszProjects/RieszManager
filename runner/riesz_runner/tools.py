"""Riesz Manager's own tools, the same for every harness.

Each session the runner hosts is started with a stdio MCP server named "riesz"
(tool_server.py), mounted through its harness's official MCP configuration
(Claude Code: --mcp-config; the drivers: Driver.mount_tools). The server only
relays: the tools run here, for the session whose harness process started it.
Which session that is comes from the caller's process ancestry (the runner
started that harness process), so no session id or token is handed to the harness.

Adding a tool: one `Tool(...)` in TOOLS. Every harness gets it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import struct
import uuid as uuidlib
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .drivers.base import ToolServer
from .harnesses import HARNESSES
from .nodes import NodeError
from .notes import COLORS as NOTE_COLORS, NoteError
from .shares import ShareError

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

SERVER_NAME = "riesz"
INSTRUCTIONS = ("Riesz Manager 托管着这个会话。这里的工具可以定时唤醒本会话（到点给它发一条消息，重启后也有效）、"
                "给别的会话发消息、开新会话、查看别的会话的状态和回复、把文件发给用户、"
                "用 note_write 给用户写便签（持续显示在网页顶栏的浮动便签窗口里，不随上下文覆盖）、"
                "以及当用户把自己的本地电脑节点绑定到本会话时，通过 node_info/node_exec/node_push/node_pull 在该电脑上执行命令和互传文件。"
                "如果同时挂载了 `browser`（chrome-devtools-mcp）服务器，它连接的就是本机常驻的已登录 Chrome 浏览器，请直接用它操作网页（若目标网站会话过期，直接点页面登录即可），不要为此通过 subagent_start 开子代理。")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MIN_PREFIX = 6


def server(cfg: Any) -> ToolServer:
    """How a harness starts the tool server: this runner's Python, isolated (-I: only the standard library)."""
    script = Path(__file__).with_name("tool_server.py")
    return ToolServer(name=SERVER_NAME, command=sys.executable,
                      args=("-I", str(script), "--socket", str(cfg.socket_path)),
                      config_dir=str(cfg.state_dir / "tools"))


class ToolError(Exception):
    """Shown to the agent as the tool's (error) result."""


# ---- who is calling -------------------------------------------------------------------------

def peer_pid(transport: Any) -> int | None:
    """The process at the other end of a Unix socket connection (SO_PEERCRED), if it is this user's."""
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None or sock.family != socket.AF_UNIX:
        return None
    try:
        pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    except OSError:
        return None
    return pid if uid == os.getuid() and pid > 0 else None


def ancestry(pid: int, limit: int = 32) -> list[int]:
    """The process and its parents, nearest first."""
    out = []
    while pid > 1 and len(out) < limit:
        out.append(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        pid = int(stat[stat.rindex(")") + 2:].split()[1])      # the name may hold spaces and ")"
    return out


def caller_session(hub: Hub, pid: int | None) -> str | None:
    """The hosted session whose harness process `pid` descends from."""
    if pid is None:
        return None
    owners = {p.pid: sid for sid, p in hub.hosted.items() if p.alive and p.pid}
    owners.update({d.pid: sid for sid, d in hub.harnesses.drivers.items() if d.alive and d.pid})
    return next((owners[p] for p in ancestry(pid) if p in owners), None)


# ---- the runner's own API, as rieszctl uses it ------------------------------------------------------------

class Api:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    async def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        cfg = self.hub.cfg
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(cfg.socket_path)),
                                         timeout=aiohttp.ClientTimeout(total=180)) as http:
            async with http.request(method, "http://runner" + path, json=body,
                                    headers={"Authorization": f"Bearer {cfg.token}"}) as res:
                data = await res.json(content_type=None)
                if res.status >= 400:
                    raise ToolError((data or {}).get("error") or f"HTTP {res.status}")
                return data


@dataclass
class Call:
    hub: Hub
    session_id: str           # the calling session
    api: Api

    def cwd(self) -> str | None:
        proc = self.hub.hosted.get(self.session_id)
        if proc is not None:
            return proc.cwd
        return (self.hub.harnesses.records.get(self.session_id) or {}).get("cwd")

    async def sessions(self) -> list[dict[str, Any]]:
        return (await self.api("GET", "/v1/sessions?limit=2000")).get("sessions", [])

    async def resolve(self, ident: Any) -> str:
        ident = str(ident or "").strip().lower()
        if UUID_RE.match(ident):
            return ident
        if len(ident) < MIN_PREFIX or not re.fullmatch(r"[0-9a-f-]+", ident):
            raise ToolError(f"不是会话 ID：{ident!r}（至少写前 {MIN_PREFIX} 位）")
        found = [s["sessionId"] for s in await self.sessions() if s.get("sessionId", "").startswith(ident)]
        if not found:
            raise ToolError(f"没有以 {ident} 开头的会话")
        if len(found) > 1:
            raise ToolError(f"{ident} 对应多个会话：" + "、".join(f[:8] for f in found[:5]))
        return found[0]

    async def other(self, ident: Any) -> str:
        sid = await self.resolve(ident)
        sid = self.hub.final_handoff_target(sid)
        if sid == self.hub.final_handoff_target(self.session_id):
            raise ToolError("这是你自己所在的会话")
        return sid


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict[str, Any]                     # JSON Schema properties
    run: Callable[[Call, dict[str, Any]], Awaitable[str]]
    required: tuple[str, ...] = ()
    read_only: bool = False

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": {"type": "object", "properties": self.params, "required": list(self.required)},
                "annotations": {"readOnlyHint": self.read_only, "destructiveHint": False, "openWorldHint": False}}


# ---- schedules ---------------------------------------------------------------------------------

def _minutes(value: Any, what: str) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value)
    m = re.fullmatch(r"\s*(\d+)\s*(m|min|h)?\s*", str(value or ""))
    if not m or int(m.group(1)) <= 0:
        raise ToolError(f"{what} 写成 30m、2h 或分钟数，收到的是 {value!r}")
    return int(m.group(1)) * (60 if m.group(2) == "h" else 1)


def _schedule_line(s: dict[str, Any]) -> str:
    nxt = time.strftime("%m-%d %H:%M", time.localtime(s["next"])) if s.get("next") else "无"
    parts = [f"{s['id']}  会话 {s['sessionId'][:8]}  下次 {nxt}  {s.get('rule') or ''}"]
    if not s.get("enabled"):
        parts.append("（已暂停）")
    if s.get("name"):
        parts.append(f"名字：{s['name']}")
    if s.get("lastResult"):
        parts.append(f"上次：{s['lastResult']}")
    return "  ".join(parts)


async def schedule_add(call: Call, a: dict[str, Any]) -> str:
    message = str(a.get("message") or "").strip()
    if not message:
        raise ToolError("message 不能为空")
    sid = await call.resolve(a["session_id"]) if a.get("session_id") else call.session_id
    body: dict[str, Any] = {"sessionId": sid, "prompt": message, "catchUp": bool(a.get("catch_up")),
                            "createdBy": call.session_id}
    at, every = a.get("at"), a.get("every")
    if bool(at) == bool(every):
        raise ToolError("at（几点）和 every（每隔多久）要给且只给一个")
    if at:
        times = at if isinstance(at, list) else str(at).split(",")
        body["times"] = [str(t).strip() for t in times if str(t).strip()]
        if a.get("days"):
            days = a["days"] if isinstance(a["days"], list) else str(a["days"]).split(",")
            try:
                body["days"] = [DAYS[str(d).strip().lower()[:3]] for d in days if str(d).strip()]
            except KeyError:
                raise ToolError("days 用 mon、tue、wed、thu、fri、sat、sun") from None
    else:
        body["every"] = _minutes(every, "every")
    if a.get("jitter"):
        body["jitter"] = _minutes(a["jitter"], "jitter")
    for key in ("name", "tz"):
        if a.get(key):
            body[key] = str(a[key])
    s = (await call.api("POST", "/v1/schedules", body))["schedule"]
    return "已添加：\n" + _schedule_line(s)


async def schedule_list(call: Call, a: dict[str, Any]) -> str:
    query = "" if a.get("all") else f"?session={call.session_id}"
    items = (await call.api("GET", "/v1/schedules" + query))["schedules"]
    return "\n".join(_schedule_line(s) for s in items) if items else "没有定时"


SCHEDULE_ACTIONS = {"pause": ("update", {"enabled": False}), "resume": ("update", {"enabled": True}),
                    "run": ("run", None), "remove": ("delete", None)}


async def schedule_change(call: Call, a: dict[str, Any]) -> str:
    action = str(a.get("action") or "")
    if action not in SCHEDULE_ACTIONS:
        raise ToolError("action 是 pause、resume、run 或 remove")
    sched_id = str(a.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sched_id):
        raise ToolError("id 是 schedule_list 列出的定时 ID")
    path, body = SCHEDULE_ACTIONS[action]
    res = await call.api("POST", f"/v1/schedules/{sched_id}/{path}", body or {})
    if "schedule" in res:
        return _schedule_line(res["schedule"])
    return f"已删除 {sched_id}"


# ---- other sessions ---------------------------------------------------------------------------

def _state_word(summary: dict[str, Any]) -> str:
    live = summary.get("live") or {}
    if live.get("waiting"):
        return "等用户批准"
    if live.get("busy"):
        return "工作中"
    return "空闲" if live else "未运行"


def _title(s: dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", s.get("title") or s.get("customTitle") or s.get("firstPrompt") or "")[:60]


def _matches_dir(cwd: str, raw_filter: str) -> bool:
    if not cwd:
        return False
    needle = raw_filter.strip()
    if not needle:
        return True
    expanded = os.path.expanduser(needle)
    if os.path.isabs(expanded):
        target = os.path.realpath(expanded)
        cand = os.path.realpath(cwd) if os.path.exists(cwd) else cwd
        if cand == target or cand.startswith(target.rstrip(os.sep) + os.sep):
            return True
    return needle.lower() in cwd.lower()


async def session_list(call: Call, a: dict[str, Any]) -> str:
    sessions = [s for s in await call.sessions() if not s.get("subagentOf")]
    directory = str(a.get("directory") or "").strip()
    query = str(a.get("query") or "").strip()
    has_filter = bool(directory or query)
    if directory:
        sessions = [s for s in sessions if _matches_dir(str(s.get("cwd") or ""), directory)]
    if query:
        q = query.lower()
        sessions = [
            s for s in sessions
            if q in _title(s).lower()
            or q in str(s.get("cwd") or "").lower()
            or q in str(s.get("sessionId") or "").lower()
            or q in str(s.get("harness") or "claude").lower()
        ]
    if not a.get("all") and not has_filter:
        sessions = [s for s in sessions if s.get("live") or s.get("keepAlive") or s.get("sessionId") == call.session_id]
    raw_limit = a.get("limit")
    limit = max(1, min(int(raw_limit), 200)) if isinstance(raw_limit, (int, float)) and not isinstance(raw_limit, bool) else 50
    total = len(sessions)
    if a.get("all") or has_filter:
        sessions = sessions[:limit]
    lines = []
    for s in sessions:
        flags = [f for f in ("常驻" if s.get("keepAlive") else "", "你自己" if s.get("sessionId") == call.session_id else "") if f]
        lines.append(f"{s['sessionId'][:8]}  {s.get('harness') or 'claude'}  {_state_word(s)}"
                     + (f"（{'，'.join(flags)}）" if flags else "") + f"  {s.get('cwd') or ''}  {_title(s)}")
    if not lines:
        return "没有匹配的会话" if has_filter else "没有会话"
    if total > len(sessions):
        lines.append(f"（共 {total} 个会话，仅显示最近 {len(sessions)} 个；可用 directory / query 过滤或调大 limit）")
    return "\n".join(lines)


async def _last_reply(call: Call, sid: str) -> str:
    items = (await call.api("GET", f"/v1/sessions/{sid}/items?limit=200")).get("items", [])
    for item in reversed(items):
        if item.get("kind") == "assistant":
            texts = [b.get("text", "") for b in item.get("blocks") or () if b.get("type") == "text" and b.get("text")]
            if texts:
                return "\n\n".join(texts).strip()
    return ""


async def session_reply(call: Call, a: dict[str, Any]) -> str:
    sid = await call.other(a.get("session_id"))
    data = await call.api("GET", f"/v1/sessions/{sid}")
    state = data.get("state") or {}
    if state.get("permissions"):
        word = "在等用户批准一个操作"
    elif state.get("busy") or state.get("queue"):
        word = "还在工作，下面是它目前最后一条回复"
    elif state.get("alive"):
        word = "空闲"
    else:
        word = "未运行"
    reply = await _last_reply(call, sid)
    return f"{sid[:8]}：{word}\n\n" + (reply or "（还没有回复）")


async def session_send(call: Call, a: dict[str, Any]) -> str:
    sid = await call.other(a.get("session_id"))
    text = str(a.get("message") or "").strip()
    if not text:
        raise ToolError("message 不能为空")
    res = await call.api("POST", f"/v1/sessions/{sid}/messages", {"text": text})
    if res.get("held"):
        return f"已发出：{sid[:8]} 正在终端里忙，空闲后会收到"
    return f"已发给 {sid[:8]}。它回复后可以用 session_reply 查看"


CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
ALL_EFFORTS = ("off", "none", "minimal", "low", "medium", "high", "xhigh", "max")


def _parse_effort(harness: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    effort = str(value).strip().lower()
    if not effort:
        return None
    if harness == "claude":
        if effort not in CLAUDE_EFFORTS:
            raise ToolError(f"claude 的 effort 只能是 {' / '.join(CLAUDE_EFFORTS)} 之一，收到的是 {value!r}")
    elif not re.fullmatch(r"[a-z]{1,16}", effort):
        raise ToolError(f"无效的 effort：{value!r}")
    return effort


async def session_new(call: Call, a: dict[str, Any]) -> str:
    text = str(a.get("message") or "").strip()
    if not text:
        raise ToolError("message 不能为空")
    cwd = os.path.realpath(os.path.expanduser(str(a.get("directory") or call.cwd() or "")))
    if not os.path.isdir(cwd):
        raise ToolError(f"目录不存在：{cwd}")
    harness = str(a.get("harness") or "claude")
    effort = _parse_effort(harness, a.get("effort"))
    body: dict[str, Any] = {"cwd": cwd, "text": text, "startedBy": call.session_id}
    for key, field in (("harness", "harness"), ("model", "model")):
        if a.get(key):
            body[field] = str(a[key])
    if effort:
        body["effort"] = effort
    if a.get("keep_running"):
        body["keepAlive"] = True
    res = await call.api("POST", "/v1/sessions", body)
    sid = res["sessionId"]
    return f"已开新会话 {sid}（目录 {cwd}），它会出现在 Riesz Manager 网页里。用 session_reply 查看它的回复"


async def send_file(call: Call, a: dict[str, Any]) -> str:
    paths = a.get("paths") or a.get("path")
    paths = [paths] if isinstance(paths, str) else list(paths or [])
    if not paths:
        raise ToolError("path 不能为空")
    cwd = call.cwd() or "/"
    sent, failed = [], []
    for raw in paths:
        full = os.path.join(cwd, os.path.expanduser(str(raw)))
        try:
            entry = call.hub.shares.share(call.session_id, full, a.get("caption"))
        except ShareError as exc:
            failed.append(str(exc))
            continue
        sent.append(f"{entry['name']}（{_size(entry['size'])}）")
    if not sent:
        raise ToolError("；".join(failed))
    return "已发给用户：" + "、".join(sent) + ("\n没发出去：" + "；".join(failed) if failed else "")


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# ---- reader sticky notes -----------------------------------------------------------------------

def _note_line(n: dict[str, Any]) -> str:
    head = f"{n['id']}"
    if n.get("title"):
        head += f"  【{n['title']}】"
    if n.get("sessionId"):
        head += f"  (会话 {n['sessionId'][:8]})"
    preview = re.sub(r"\s+", " ", n.get("content") or "").strip()
    return f"{head}\n{preview}"


async def note_write(call: Call, a: dict[str, Any]) -> str:
    content = str(a.get("content") or "").strip()
    if not content:
        raise ToolError("content 不能为空")
    title = str(a["title"]) if a.get("title") is not None else None
    note_id = str(a["id"]).strip() if a.get("id") else None
    color = str(a["color"]) if a.get("color") else None
    try:
        note, created = await call.hub.notes.upsert(call.session_id, content, title=title, note_id=note_id, color=color)
    except NoteError as exc:
        raise ToolError(str(exc)) from None
    verb = "已新建便签" if created else "已更新便签"
    label = f"「{note['title']}」" if note.get("title") else ""
    return f"{verb} {note['id']}{label}（已同步到网页顶栏的便签浮动窗口；后续更新或删除请传 id={note['id']}）"


async def note_list(call: Call, a: dict[str, Any]) -> str:
    query = "" if a.get("all") else f"?session={call.session_id}"
    items = (await call.api("GET", "/v1/notes" + query)).get("notes", [])
    return "\n\n".join(_note_line(n) for n in items) if items else "没有便签"


async def note_delete(call: Call, a: dict[str, Any]) -> str:
    note_id = str(a.get("id") or "").strip()
    if not note_id:
        raise ToolError("id 不能为空")
    try:
        removed = await call.hub.notes.delete(note_id)
    except NoteError as exc:
        raise ToolError(str(exc)) from None
    label = f"「{removed['title']}」" if removed.get("title") else ""
    return f"已删除便签 {removed['id']}{label}"


# ---- browser tasks -----------------------------------------------------------------------------------

# ---- subagents: sessions of any harness started for this one -------------------------------------------

WAIT = 8 * 60                  # answered within the call up to this long (the harnesses' MCP timeouts allow ~10 min)
HARNESS_IDS = ("claude", "codex", "dsh", "gemini", "antigravity")
SUBAGENT_PREFACE = ("[子任务，来自 {name} 会话 {caller}；这条消息是另一个 agent 发的，不是用户本人。"
                    "做完后在最后一条回复里写清结果]\n\n{task}")
FOLLOW_UP = "[来自 {name} 会话 {caller}（你是它的子代理）]\n\n{text}"
_background: set[asyncio.Task[Any]] = set()


def _keep(job: asyncio.Task[Any]) -> None:
    _background.add(job)
    job.add_done_callback(_background.discard)


def _announce(hub: Hub, sid: str) -> None:
    """Tell the web app a session's summary changed (its subagent list)."""
    summary = hub.harnesses.summary(sid) if hub.harnesses.has(sid) else hub.session_summary(sid)
    _keep(asyncio.get_running_loop().create_task(hub.broadcast_global({"ev": "sessions", "sessions": [summary], "removed": []})))


def _harness_name(hub: Hub, sid: str) -> str:
    harness = hub.harnesses.harness_of(sid) or "claude"
    return "Claude Code" if harness == "claude" else HARNESSES[harness].name


async def _last_reply_item(hub: Hub, sid: str, *, include_error: bool = False) -> tuple[str | None, str]:
    """Latest assistant reply after the last user message: `(item_key, text)` (or `(None, "")`)."""
    if hub.harnesses.has(sid):
        items = hub.harnesses.items(sid)
    else:
        view = await hub.catch_up(sid)
        items = view.items if view is not None else []
    error_note: tuple[str | None, str] | None = None
    for item in reversed(items):
        kind = item.get("kind")
        if kind == "user":
            break
        if kind == "assistant":
            texts = [b.get("text", "") for b in item.get("blocks") or () if b.get("type") == "text" and b.get("text")]
            if texts:
                key = str(item.get("key") or item.get("uuid") or "") or None
                return key, "\n\n".join(texts).strip()
        elif include_error and error_note is None and kind == "notice" and item.get("level") == "error":
            text = str(item.get("text") or "").strip()
            if text:
                key = str(item.get("key") or item.get("uuid") or "") or None
                error_note = (key, text)
    return error_note or (None, "")


def _reply_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


class SubagentWatcher:
    """Delivers subagent turn replies to the parent session, including autonomous follow-up turns
    after background commands or timers finish (e.g. Antigravity waking up on its own).

    Pending callbacks are persisted to `state_dir/subagent_callbacks.json` and retried with backoff
    if parent delivery fails transiently or the runner restarts before delivery succeeds."""

    RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
    MAX_ATTEMPTS = 24
    MAX_DELIVERED = 500
    PERMANENT_ERRORS = ("unknown session", "working directory no longer exists")

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._path = Path(hub.cfg.state_dir) / "subagent_callbacks.json"
        self._sync: dict[str, asyncio.Event] = {}
        self._sync_saw_result: set[str] = set()
        self._delivered: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._reported_this_prompt: set[str] = set()
        self._stop_after: set[str] = set()
        self._deferred: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._in_flight: set[str] = set()
        self._wakeup = asyncio.Event()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        delivered = raw.get("delivered")
        if isinstance(delivered, dict):
            self._delivered = {k: v for k, v in delivered.items() if isinstance(k, str) and isinstance(v, dict)}
        pending = raw.get("pending")
        if isinstance(pending, dict):
            now = time.time()
            for child, entry in pending.items():
                if isinstance(child, str) and isinstance(entry, dict) and entry.get("parent") and entry.get("text"):
                    entry = dict(entry)
                    # Retry promptly after runner restart.
                    entry["next_at"] = min(float(entry.get("next_at") or now), now + 1.0)
                    self._pending[child] = entry
        deferred = raw.get("deferred")
        if isinstance(deferred, list):
            self._deferred = {c for c in deferred if isinstance(c, str)}

    def _save(self) -> None:
        if len(self._delivered) > self.MAX_DELIVERED:
            ordered = sorted(self._delivered.items(), key=lambda kv: float(kv[1].get("ts") or 0.0))
            self._delivered = dict(ordered[-self.MAX_DELIVERED:])
        payload = {"delivered": self._delivered, "pending": self._pending, "deferred": sorted(self._deferred)}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("could not save subagent callback state: %s", exc)

    def _is_delivered(self, child: str, key: str | None, text: str) -> bool:
        prev = self._delivered.get(child)
        if not prev:
            return False
        return prev.get("key") == key and prev.get("hash") == _reply_hash(text)

    def _mark_delivered(self, child: str, key: str | None, text: str) -> None:
        self._delivered[child] = {"key": key, "hash": _reply_hash(text), "ts": time.time()}
        self._reported_this_prompt.add(child)

    def has_in_flight(self) -> bool:
        return bool(self._in_flight)

    def _lock(self, child: str) -> asyncio.Lock:
        lock = self._locks.get(child)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[child] = lock
        return lock

    def _parent_of(self, child: str) -> tuple[str, dict[str, Any]] | tuple[None, dict[str, Any]]:
        pref = self.hub.prefs.get(child) or {}
        parent = pref.get("subagentOf")
        return (parent if isinstance(parent, str) and parent else None), pref

    def prepare_prompt(self, child: str, *, sync_wait: bool, stop_after: bool = False) -> None:
        """Register before sending a prompt to `child`, so fast completions cannot race."""
        self._reported_this_prompt.discard(child)
        self._deferred.discard(child)
        if child in self._pending:
            self._pending.pop(child, None)
            self._save()
        if stop_after:
            self._stop_after.add(child)
        if sync_wait:
            self._sync[child] = asyncio.Event()
            self._sync_saw_result.discard(child)

    async def wait_sync(self, child: str, seconds: float) -> bool:
        """Wait up to `seconds` for `child` to finish its turn and any open background tasks."""
        ev = self._sync.get(child)
        start = time.time()
        seen_work = False
        while time.time() - start < seconds:
            if self.hub.quota_fallback.resuming(child):
                # Its turn failed on quota and is being carried on (on DeepSeek): the reply is still to come.
                seen_work = True
                await asyncio.sleep(1.5)
                continue
            working = self.hub.session_working(child)
            if working is None:                      # not running (any more)
                return True
            if working:
                seen_work = True
            elif child in self._sync_saw_result or seen_work or time.time() - start > 120:
                return True
            if ev is not None:
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(1.5)
        return False

    async def _stop_child(self, child: str) -> None:
        with contextlib.suppress(Exception):
            if self.hub.harnesses.has(child):
                await self.hub.harnesses.stop(child)
            else:
                await self.hub.stop_process(child)

    async def finish_sync(self, child: str, finished: bool) -> str | None:
        """End a synchronous wait. Returns the reply text if finished, or None to hand off to async callbacks."""
        async with self._lock(child):
            self._sync.pop(child, None)
            self._sync_saw_result.discard(child)
            self._deferred.discard(child)
            parent, _ = self._parent_of(child)
            if finished:
                key, text = await _last_reply_item(self.hub, child, include_error=True)
                if text:
                    self._mark_delivered(child, key, text)
                    self._pending.pop(child, None)
                    self._save()
                if child in self._stop_after:
                    self._stop_after.discard(child)
                    await self._stop_child(child)
                if parent:
                    _announce(self.hub, parent)
                return text or "（它没有给出回复）"
            # Timed out: if the child finished in the tiny window before we left _sync, deliver asynchronously.
            if not self.hub.session_working(child):
                _keep(asyncio.get_running_loop().create_task(self._deliver(child, {})))
            return None

    def on_result(self, child: str, event: dict[str, Any]) -> None:
        """Called whenever any hosted session finishes a turn (prompted or autonomous)."""
        parent, _ = self._parent_of(child)
        if not parent:
            return
        _announce(self.hub, parent)
        ev = self._sync.get(child)
        if ev is not None:
            self._sync_saw_result.add(child)
            ev.set()
            return
        _keep(asyncio.get_running_loop().create_task(self._deliver(child, event)))

    def on_exit(self, child: str, event: dict[str, Any]) -> None:
        """Called whenever any hosted session exits."""
        parent, _ = self._parent_of(child)
        if not parent:
            return
        _announce(self.hub, parent)
        ev = self._sync.get(child)
        if ev is not None:
            self._sync_saw_result.add(child)
            ev.set()
            return
        _keep(asyncio.get_running_loop().create_task(self._deliver_exit(child, event)))

    async def _attempt_send(self, child: str) -> bool:
        """Try to deliver `self._pending[child]` once. Caller holds `self._lock(child)`."""
        entry = self._pending.get(child)
        if not entry or child in self._sync:
            return False
        if self.hub.delivery is None or self.hub.closing or self.hub.reload_ready.is_set():
            return False
        parent = entry["parent"]
        target = self.hub.final_handoff_target(parent)
        payload = f"{entry['header']}\n\n{entry['text']}" if entry.get("header") else str(entry["text"])
        self._in_flight.add(child)
        try:
            await self.hub.delivery.send(target, payload, [], str(uuidlib.uuid4()))
        except Exception as exc:  # noqa: BLE001
            attempts = int(entry.get("attempts") or 0) + 1
            entry["attempts"] = attempts
            msg = str(exc).lower()
            permanent = any(marker in msg for marker in self.PERMANENT_ERRORS)
            if permanent or attempts >= self.MAX_ATTEMPTS:
                log.warning("subagent callback %s -> %s dropped after %d attempt(s): %s",
                            child[:8], target[:8], attempts, exc)
                self._pending.pop(child, None)
            else:
                delay = self.RETRY_DELAYS[min(attempts - 1, len(self.RETRY_DELAYS) - 1)]
                entry["next_at"] = time.time() + delay
                log.warning("subagent callback %s -> %s failed (attempt %d): %s; retrying in %.0fs",
                            child[:8], target[:8], attempts, exc, delay)
                self._wakeup.set()
            self._save()
            return False
        finally:
            self._in_flight.discard(child)
        self._pending.pop(child, None)
        if not entry.get("is_exit_error"):
            self._mark_delivered(child, entry.get("key"), str(entry["text"]))
        self._save()
        _announce(self.hub, parent)
        return True

    async def _deliver(self, child: str, event: dict[str, Any]) -> None:
        async with self._lock(child):
            if child in self._sync:
                return
            parent, pref = self._parent_of(child)
            if not parent:
                return
            # Still busy, queued, waiting on a permission prompt, or running open background tasks:
            # defer delivery until either a later autonomous turn finishes or flush_due sees work end.
            if self.hub.session_working(child):
                if child not in self._deferred:
                    self._deferred.add(child)
                    self._save()
                return
            was_deferred = child in self._deferred
            self._deferred.discard(child)
            is_error = bool((event.get("result") or {}).get("isError"))
            key, text = await _last_reply_item(self.hub, child, include_error=is_error)
            if not text or self._is_delivered(child, key, text):
                if was_deferred:
                    self._save()
                return
            is_followup = child in self._reported_this_prompt
            label = "浏览器任务" if pref.get("browser") else "子代理"
            header = (f"[{label} {child[:8]} 后台任务完成后又更新了回复，最新结果如下]"
                      if is_followup else f"[{label} {child[:8]} 这一轮做完了，回复如下]")
            self._pending[child] = {
                "parent": parent, "header": header, "text": text, "key": key,
                "is_exit_error": False, "attempts": 0, "next_at": 0.0, "created_at": time.time(),
            }
            self._save()
            if child in self._stop_after:
                self._stop_after.discard(child)
                await self._stop_child(child)
            await self._attempt_send(child)

    async def _deliver_exit(self, child: str, event: dict[str, Any]) -> None:
        async with self._lock(child):
            self._deferred.discard(child)
            if child in self._sync or event.get("requested"):
                return
            parent, pref = self._parent_of(child)
            if not parent:
                return
            key, text = await _last_reply_item(self.hub, child, include_error=True)
            label = "浏览器任务" if pref.get("browser") else "子代理"
            if text and not self._is_delivered(child, key, text):
                is_followup = child in self._reported_this_prompt
                header = (f"[{label} {child[:8]} 后台任务完成后又更新了回复，最新结果如下]"
                          if is_followup else f"[{label} {child[:8]} 这一轮做完了，回复如下]")
                self._pending[child] = {
                    "parent": parent, "header": header, "text": text, "key": key,
                    "is_exit_error": False, "attempts": 0, "next_at": 0.0, "created_at": time.time(),
                }
                self._save()
                await self._attempt_send(child)
            elif event.get("duringTurn") or event.get("code") not in (0, None):
                code = event.get("code")
                msg = f"[{label} {child[:8]} 进程异常退出（代码 {code}），未能完成这一轮]"
                self._pending[child] = {
                    "parent": parent, "header": "", "text": msg, "key": None,
                    "is_exit_error": True, "attempts": 0, "next_at": 0.0, "created_at": time.time(),
                }
                self._save()
                await self._attempt_send(child)
            else:
                _announce(self.hub, parent)

    async def flush_due(self) -> None:
        """Deliver deferred completions once their background work ends, and retry due pending callbacks."""
        for child in list(self._deferred):
            if child in self._sync or not self._parent_of(child)[0]:
                self._deferred.discard(child)
                continue
            if not self.hub.session_working(child):
                await self._deliver(child, {})
        now = time.time()
        for child, entry in list(self._pending.items()):
            if float(entry.get("next_at") or 0.0) <= now:
                async with self._lock(child):
                    await self._attempt_send(child)

    async def loop(self) -> None:
        """Background retry loop (also flushes callbacks persisted before a runner restart)."""
        await asyncio.sleep(1.0)
        while True:
            try:
                await self.flush_due()
            except Exception:  # noqa: BLE001
                log.exception("subagent callback retry loop error")
            self._wakeup.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=2.0)


async def _reply_when_done(call: Call, child: str, wait: bool, label: str) -> str:
    """The child's reply once its turn ends: within the call (up to WAIT), or later via SubagentWatcher."""
    watcher = call.hub.subagent_watcher
    if wait:
        finished = await watcher.wait_sync(child, WAIT)
        text = await watcher.finish_sync(child, finished)
        if text is not None:
            return text
    return f"{label} {child[:8]} 还在做（可能在等用户批准）。做完后它的回复会作为一条消息发给你，不用反复查。"


async def _start_child(call: Call, harness: str, text: str, label: str, *, mode: str | None, model: str | None = None,
                       effort: str | None = None, directory: str | None = None, browser: bool = False,
                       sync_wait: bool = True, stop_after: bool = False) -> str:
    hub = call.hub
    if (hub.prefs.get(call.session_id) or {}).get("subagentOf"):
        raise ToolError("子代理里不能再开子代理")
    cwd = os.path.realpath(os.path.expanduser(str(directory or call.cwd() or hub.cfg.workspace_root)))
    if not os.path.isdir(cwd):
        raise ToolError(f"目录不存在：{cwd}")
    try:
        if harness == "claude":
            proc = await hub.create_session(cwd, model=model, permission_mode=mode, effort=effort, browser=browser)
            child = proc.session_id
        else:
            child = str((await hub.harnesses.create(harness, cwd, model=model, effort=effort, mode=mode)).session_id)
    except Exception as exc:  # noqa: BLE001 - why the harness could not start, for the agent
        raise ToolError(f"{harness} 没能启动：{exc}") from None
    hub.prefs.remember(child, startedBy=call.session_id, subagentOf=call.session_id, subagentTask=label[:200],
                       browser="1" if browser else None)
    with contextlib.suppress(ToolError):
        await call.api("POST", f"/v1/sessions/{child}/rename", {"title": label[:60]})
    hub.subagent_watcher.prepare_prompt(child, sync_wait=sync_wait, stop_after=stop_after)
    try:
        if harness == "claude":
            await hub.delivery.send_new(proc, text, [], str(uuidlib.uuid4()))
        else:
            await hub.delivery.send(child, text, [], str(uuidlib.uuid4()))
    except Exception:
        if sync_wait:
            await hub.subagent_watcher.finish_sync(child, False)
        raise
    _announce(hub, call.session_id)
    return child


async def subagent_start(call: Call, a: dict[str, Any]) -> str:
    hub = call.hub
    task = str(a.get("task") or "").strip()
    harness = str(a.get("harness") or "claude")
    if not task:
        raise ToolError("task 不能为空")
    if harness != "claude" and harness not in HARNESSES:
        raise ToolError(f"harness 是 {'、'.join(HARNESS_IDS)} 之一")
    effort = _parse_effort(harness, a.get("effort"))
    # As permissive as the caller: its mode's tier, in the target harness's own mode for that tier.
    caller = call.session_id
    if hub.harnesses.has(caller):
        summary = hub.harnesses.summary(caller)
    else:                                       # Claude Code: its mode now is in the running process's state
        summary = {**hub.session_summary(caller), "permissionMode": (hub.session_state(caller) or {}).get("permissionMode")}
    mode = hub._closest_mode(harness, hub._mode_tier(caller, summary))
    wait = a.get("wait", True) is not False
    text = SUBAGENT_PREFACE.format(name=_harness_name(hub, caller), caller=caller[:8], task=task)
    child = await _start_child(call, harness, text, task, mode=mode, model=a.get("model") or None,
                               effort=effort, directory=a.get("directory"), sync_wait=wait)
    reply = await _reply_when_done(call, child, wait, "子代理")
    return f"子代理 {child[:8]}（{_harness_name(hub, child)}）：\n\n{reply}\n\n（接着跟它说话用 subagent_send，agent_id={child[:8]}）"


async def subagent_send(call: Call, a: dict[str, Any]) -> str:
    hub = call.hub
    child = hub.final_handoff_target(await call.resolve(a.get("agent_id")))
    owner = (hub.prefs.get(child) or {}).get("subagentOf")
    if not owner or hub.final_handoff_target(owner) != hub.final_handoff_target(call.session_id):
        raise ToolError("这不是你开的子代理（用 subagent_start 开的才行）")
    text = str(a.get("message") or "").strip()
    if not text:
        raise ToolError("message 不能为空")
    wait = a.get("wait", True) is not False
    hub.subagent_watcher.prepare_prompt(child, sync_wait=wait)
    try:
        await hub.delivery.send(child, FOLLOW_UP.format(name=_harness_name(hub, call.session_id), caller=call.session_id[:8], text=text),
                                [], str(uuidlib.uuid4()))
    except Exception:
        if wait:
            await hub.subagent_watcher.finish_sync(child, False)
        raise
    _announce(hub, call.session_id)
    reply = await _reply_when_done(call, child, wait, "子代理")
    return f"子代理 {child[:8]}（{_harness_name(hub, child)}）：\n\n{reply}"


# ---- bound local computer nodes (nodes.py) --------------------------------------------------

def _session_cwd(call: Call) -> Path:
    summary = call.hub.session_summary(call.session_id) or {}
    cwd = summary.get("cwd")
    return Path(cwd) if cwd else call.hub.cfg.workspace_root


def _resolve_server_path(call: Call, raw: str, *, for_write: bool = False) -> Path:
    p = Path(os.path.expanduser(raw.strip()))
    if not p.is_absolute():
        p = _session_cwd(call) / p
    resolved = Path(os.path.realpath(p))
    allowed_roots = [
        Path(os.path.realpath(call.hub.cfg.workspace_root)),
        Path(os.path.realpath(_session_cwd(call))),
        Path(os.path.realpath("/tmp")),
        Path(os.path.realpath(call.hub.cfg.state_dir / "node_pulls")),
    ]
    if not for_write:
        allowed_roots.extend([
            Path(os.path.realpath(call.hub.cfg.state_dir / "shared")),
            Path(os.path.realpath(call.hub.cfg.state_dir / "uploads")),
        ])
    for root in allowed_roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise ToolError(f"服务器路径超出允许范围：{raw}")


async def node_info(call: Call, a: dict[str, Any]) -> str:
    nodes = call.hub.nodes.session_nodes(call.session_id)
    if not nodes:
        return "当前会话尚未连接任何本地电脑节点。若需要操控用户的个人电脑，请提醒用户在本会话中点击「本地电脑」生成配对码并连接。"
    lines = ["当前会话已连接的本地电脑节点："]
    for live in nodes:
        dirs = "、".join(live.dirs) if live.dirs else "（尚未授权目录，等待用户在电脑端添加）"
        note = f" | 备注：{live.note}" if live.note else ""
        lines.append(f"- {live.name} (id={live.node_id}, os={live.os_name or live.platform}, 权限={live.mode}, 允许目录={dirs}{note})")
    lines.append("\n注意：本地电脑与服务器不在同一磁盘，且用户可随时断开；传文件请用 node_push / node_pull，执行命令用 node_exec。")
    return "\n".join(lines)


async def node_exec(call: Call, a: dict[str, Any]) -> str:
    try:
        res = await call.hub.nodes.exec_command(
            call.session_id,
            command=str(a.get("command") or ""),
            cwd=str(a.get("cwd") or ""),
            timeout=int(a.get("timeout") or 120),
            node_ref=str(a.get("node") or "") or None,
        )
    except NodeError as exc:
        raise ToolError(str(exc)) from None
    head = f"[本地节点 {res['node']}] exit={res['exitCode']}"
    if res.get("killed"):
        head += " (已终止)"
    if res.get("riskReason"):
        head += f" | 风控({res['risk']}): {res['riskReason']}"
    body = (res.get("output") or "").strip()
    if len(body) > 24000:
        body = body[:8000] + "\n... [中间输出已截断] ...\n" + body[-12000:]
    return f"{head}\n\n{body}" if body else f"{head}\n（无输出）"


async def node_push(call: Call, a: dict[str, Any]) -> str:
    src = _resolve_server_path(call, str(a.get("server_path") or ""), for_write=False)
    dest = str(a.get("node_path") or "").strip()
    if not dest:
        raise ToolError("node_path 不能为空")
    try:
        res = await call.hub.nodes.push_file(
            call.session_id, server_path=src, dest_path=dest,
            node_ref=str(a.get("node") or "") or None,
        )
    except NodeError as exc:
        raise ToolError(str(exc)) from None
    return f"已推送到 {res['node']}:{res['destPath']}（{res['size']} 字节，sha256={res['sha256'][:12]}）"


async def node_pull(call: Call, a: dict[str, Any]) -> str:
    src = str(a.get("node_path") or "").strip()
    if not src:
        raise ToolError("node_path 不能为空")
    raw_dest = str(a.get("server_path") or "").strip()
    if not raw_dest:
        base_name = os.path.basename(src.replace("\\", "/").rstrip("/"))
        if base_name in ("", ".", "..") or any(ord(c) < 32 for c in base_name):
            base_name = f"file-{uuidlib.uuid4().hex[:6]}"
        dest = call.hub.cfg.state_dir / "node_pulls" / call.session_id[:12] / base_name
    else:
        dest = _resolve_server_path(call, raw_dest, for_write=True)
    try:
        res = await call.hub.nodes.pull_file(
            call.session_id, node_path=src, server_dest=dest,
            node_ref=str(a.get("node") or "") or None,
        )
    except NodeError as exc:
        raise ToolError(str(exc)) from None
    return f"已从 {res['node']} 拉取到服务器：{res['serverPath']}（{res['size']} 字节，sha256={str(res['sha256'])[:12]}）"


_SESSION_ID = {"type": "string", "description": "会话 ID（可以只写前 8 位）"}
_EFFORT = {
    "type": "string",
    "enum": list(ALL_EFFORTS),
    "description": "思考强度（可选）：claude 支持 low/medium/high/xhigh/max；codex 支持 minimal/low/medium/high/xhigh；"
                   "dsh 支持 off/low/high/max；antigravity/gemini 思考档位含在 model 名里",
}

TOOLS: list[Tool] = [
    Tool("subagent_start",
         "开一个子代理：用指定的工具（claude=Claude Code、codex、dsh=DeepSeek Harness、gemini=Gemini CLI、antigravity）"
         "开一个会话替你做一件事，等它这一轮做完把回复交给你。它显示在你的子代理列表里，权限和你一样宽松。"
         "之后用 subagent_send 接着跟它说话。最多等 8 分钟；超过或 wait=false 时先返回，做完后它的回复作为一条消息发给你。"
         "网页操作请直接使用已挂载的 browser 工具，不要用 subagent_start 开浏览器子代理。",
         {"task": {"type": "string", "description": "交给它的事，写清楚背景、要求和要交回什么"},
          "harness": {"type": "string", "enum": list(HARNESS_IDS), "description": "用哪个工具（默认 claude）"},
          "model": {"type": "string", "description": "模型（不写就用该工具的默认）"},
          "effort": _EFFORT,
          "directory": {"type": "string", "description": "工作目录（默认是你的）"},
          "wait": {"type": "boolean", "description": "是否等它做完（默认 true）"}},
         subagent_start, required=("task",)),
    Tool("subagent_send", "给你用 subagent_start 开的子代理发一条消息（接着交流），等它这一轮做完把回复交给你（规则同 subagent_start）。",
         {"agent_id": {"type": "string", "description": "subagent_start 返回的 agent_id"},
          "message": {"type": "string"}, "wait": {"type": "boolean", "description": "是否等它做完（默认 true）"}},
         subagent_send, required=("agent_id", "message")),
    Tool("send_file",
         "把文件发给用户：图片、PDF、文档、表格、任何文件都行，会显示在用户的对话里（图片直接显示，其他文件可以点开查看或下载）。"
         "想让用户看一个文件时用它，不要只在回复里写路径或 file:// 链接。",
         {"path": {"type": "string", "description": "文件路径（相对路径按你的工作目录算）"},
          "paths": {"type": "array", "items": {"type": "string"}, "description": "一次发多个文件时用"},
          "caption": {"type": "string", "description": "附一句说明（可选）"}},
         send_file),
    Tool("note_write",
         "给用户写一条便签：持续显示在网页顶栏的浮动便签小窗口里，不会随聊天滚走或被后续上下文覆盖。"
         "适合放需要用户反复对照看的短信息（例如待选项、重要结论、操作步骤、端口/命令备忘等），可以建多条，每条建议精炼在几百字以内。"
         "传 id 可更新已有便签。",
         {"content": {"type": "string", "description": "便签正文（支持 Markdown，建议精炼在几百字以内）"},
          "title": {"type": "string", "description": "便签标题（可选，简短概括主题）"},
          "id": {"type": "string", "description": "要更新的已有便签 ID；不传则新建一条"},
          "color": {"type": "string", "enum": list(NOTE_COLORS), "description": "色标（默认 amber；amber=重点，sage=完成/参考，slate=参数/信息，rose=注意/警告）"}},
         note_write, required=("content",)),
    Tool("note_list", "列出当前会话写给用户的便签（all 为真时列出所有会话的便签），含 ID、标题和内容。",
         {"all": {"type": "boolean"}}, note_list, read_only=True),
    Tool("note_delete", "删除一条不再需要给用户展示的便签。",
         {"id": {"type": "string", "description": "note_write 或 note_list 返回的便签 ID"}},
         note_delete, required=("id",)),
    Tool("node_info",
         "查看当前会话绑定了哪些在线的本地电脑节点（OS、允许目录白名单、权限模式、备注）。"
         "本地电脑与服务器环境不同且用户可随时断开；未绑定时无法操控用户电脑。",
         {}, node_info, read_only=True),
    Tool("node_exec",
         "在当前会话绑定的本地电脑节点上执行命令（工作目录必须在节点允许的白名单目录内；受权限模式与风险检查约束，高风险或 ask 模式下会等用户审批）。",
         {"command": {"type": "string", "description": "要在本地电脑执行的命令"},
          "cwd": {"type": "string", "description": "本地电脑上的工作目录（不填默认使用节点第一个允许目录）"},
          "timeout": {"type": "integer", "description": "超时秒数（默认 120，最大 600）"},
          "node": {"type": "string", "description": "当绑定了多个节点时，指定节点名或 ID 前缀"}},
         node_exec, required=("command",)),
    Tool("node_push",
         "把 Riesz Manager 服务器上的文件推送到当前会话绑定的本地电脑节点（支持大文件流式传输与 SHA-256 校验，目标路径须在节点允许目录内）。",
         {"server_path": {"type": "string", "description": "服务器上的源文件路径（相对路径按当前会话工作目录算）"},
          "node_path": {"type": "string", "description": "本地电脑上的目标路径（绝对路径，或相对于节点首个允许目录的相对路径）"},
          "node": {"type": "string", "description": "当绑定了多个节点时，指定节点名或 ID 前缀"}},
         node_push, required=("server_path", "node_path")),
    Tool("node_pull",
         "从当前会话绑定的本地电脑节点拉取文件（如截图、日志、构建产物）到服务器，返回服务器保存路径（随后可读取或用 send_file 发给用户）。",
         {"node_path": {"type": "string", "description": "本地电脑上的源文件路径（须在节点允许目录内）"},
          "server_path": {"type": "string", "description": "保存到服务器的路径（可选，不填则保存到临时拉取目录并返回完整路径）"},
          "node": {"type": "string", "description": "当绑定了多个节点时，指定节点名或 ID 前缀"}},
         node_pull, required=("node_path",)),
    Tool("schedule_add",
         "定时唤醒：到点时 Riesz Manager 给会话（默认是你自己所在的会话）发一条消息，会话没在运行就先启动它。"
         "定时保存在 Riesz Manager 里，重启、空闲关闭后都有效。at（每天几点）和 every（每隔多久）二选一。",
         {"message": {"type": "string", "description": "到点时发的消息；其中的 {now} 会换成当时的时间"},
          "at": {"type": "array", "items": {"type": "string"}, "description": "每天的时间，如 [\"09:00\", \"21:30\"]"},
          "days": {"type": "array", "items": {"type": "string", "enum": list(DAYS)}, "description": "只在这几天（配合 at）"},
          "every": {"type": "string", "description": "每隔多久，如 \"30m\"、\"2h\""},
          "jitter": {"type": "string", "description": "随机推迟最多这么久，如 \"10m\""},
          "catch_up": {"type": "boolean", "description": "错过了（比如机器关着）是否补发一次"},
          "name": {"type": "string", "description": "给这个定时起的名字"},
          "tz": {"type": "string", "description": "时区，如 America/New_York（默认机器的时区）"},
          "session_id": {**_SESSION_ID, "description": "唤醒别的会话时写它的 ID；不写就是你自己"}},
         schedule_add, required=("message",)),
    Tool("schedule_list", "列出你所在会话的定时（all 为真时列出所有会话的）。",
         {"all": {"type": "boolean"}}, schedule_list, read_only=True),
    Tool("schedule_change", "暂停（pause）、恢复（resume）、立即触发一次（run）或删除（remove）一个定时。",
         {"id": {"type": "string", "description": "schedule_list 列出的定时 ID"},
          "action": {"type": "string", "enum": list(SCHEDULE_ACTIONS)}},
         schedule_change, required=("id", "action")),
    Tool("session_list",
         "列出正在运行或设为常驻的会话（all 为真时列出最近的所有会话），包括用的是哪个工具。"
         "传 directory 或 query 时自动在全部历史会话中过滤（不含子代理）。",
         {"all": {"type": "boolean", "description": "为真时列出最近的所有会话（默认仅列运行中/常驻）"},
          "directory": {"type": "string", "description": "按工作目录过滤（支持绝对路径或目录子串，匹配该目录及子目录）"},
          "query": {"type": "string", "description": "按标题、目录、工具名或会话 ID 关键词过滤（不区分大小写）"},
          "limit": {"type": "integer", "description": "最多返回条数（默认 50，上限 200）"}},
         session_list, read_only=True),
    Tool("session_send", "给另一个会话发一条消息（它没在运行会先启动）。不等回复；之后用 session_reply 查看。",
         {"session_id": _SESSION_ID, "message": {"type": "string"}}, session_send, required=("session_id", "message")),
    Tool("session_reply", "查看另一个会话的状态和它最后一条回复。",
         {"session_id": _SESSION_ID}, session_reply, required=("session_id",), read_only=True),
    Tool("session_new", "开一个独立的新会话（和用户在网页上开的一样，显示在会话列表里，不是你的子代理），发给它第一条消息。"
                        "要别的工具替你做事并把结果交回来，用 subagent_start。",
         {"message": {"type": "string", "description": "第一条消息"},
          "directory": {"type": "string", "description": "工作目录（默认是你自己的）"},
          "harness": {"type": "string", "enum": ["claude", "codex", "dsh", "gemini", "antigravity"],
                      "description": "用哪个工具（默认 claude，即 Claude Code）"},
          "model": {"type": "string"},
          "effort": _EFFORT,
          "keep_running": {"type": "boolean", "description": "设为常驻（空闲时不关闭）"}},
         session_new, required=("message",)),
]
BY_NAME = {t.name: t for t in TOOLS}


def listing() -> dict[str, Any]:
    return {"name": SERVER_NAME, "instructions": INSTRUCTIONS, "tools": [t.spec() for t in TOOLS]}


async def run(hub: Hub, session_id: str, name: str, args: Any) -> dict[str, Any]:
    """An MCP tools/call result: {"content": [{"type": "text", "text"}], "isError"}."""
    tool = BY_NAME.get(name)
    try:
        if tool is None:
            raise ToolError(f"没有这个工具：{name}")
        if not isinstance(args, dict):
            raise ToolError("参数要是一个对象")
        missing = [k for k in tool.required if args.get(k) in (None, "")]
        if missing:
            raise ToolError("缺少参数：" + "、".join(missing))
        text = await tool.run(Call(hub, session_id, Api(hub)), args)
        error = False
    except ToolError as exc:
        text, error = str(exc), True
    return {"content": [{"type": "text", "text": text}], "isError": error}
