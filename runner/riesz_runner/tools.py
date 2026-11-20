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
from .shares import ShareError

if TYPE_CHECKING:
    from .hub import Hub

SERVER_NAME = "riesz"
INSTRUCTIONS = ("Riesz Manager 托管着这个会话。这里的工具可以定时唤醒本会话（到点给它发一条消息，重启后也有效）、"
                "给别的会话发消息、开新会话、查看别的会话的状态和回复。")
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
        if sid == self.session_id:
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


async def session_list(call: Call, a: dict[str, Any]) -> str:
    sessions = await call.sessions()
    if a.get("all"):
        sessions = sessions[:50]
    else:
        sessions = [s for s in sessions if s.get("live") or s.get("keepAlive") or s.get("sessionId") == call.session_id]
    lines = []
    for s in sessions:
        flags = [f for f in ("常驻" if s.get("keepAlive") else "", "你自己" if s.get("sessionId") == call.session_id else "") if f]
        lines.append(f"{s['sessionId'][:8]}  {s.get('harness') or 'claude'}  {_state_word(s)}"
                     + (f"（{'，'.join(flags)}）" if flags else "") + f"  {s.get('cwd') or ''}  {_title(s)}")
    return "\n".join(lines) if lines else "没有会话"


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


async def session_new(call: Call, a: dict[str, Any]) -> str:
    text = str(a.get("message") or "").strip()
    if not text:
        raise ToolError("message 不能为空")
    cwd = os.path.realpath(os.path.expanduser(str(a.get("directory") or call.cwd() or "")))
    if not os.path.isdir(cwd):
        raise ToolError(f"目录不存在：{cwd}")
    body: dict[str, Any] = {"cwd": cwd, "text": text, "startedBy": call.session_id}
    for key, field in (("harness", "harness"), ("model", "model")):
        if a.get(key):
            body[field] = str(a[key])
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


# ---- browser tasks -----------------------------------------------------------------------------------

# ---- subagents: sessions of any harness started for this one -------------------------------------------

WAIT = 8 * 60                  # answered within the call up to this long (the harnesses' MCP timeouts allow ~10 min)
HARNESS_IDS = ("claude", "codex", "dsh", "gemini", "antigravity")
SUBAGENT_PREFACE = ("[子任务，来自 {name} 会话 {caller}；这条消息是另一个 agent 发的，不是用户本人。"
                    "做完后在最后一条回复里写清结果]\n\n{task}")
FOLLOW_UP = "[来自 {name} 会话 {caller}（你是它的子代理）]\n\n{text}"
BROWSER_PREFACE = ("[浏览器任务，来自 {name} 会话 {caller}；这条消息是另一个 agent 发的，不是用户本人]\n"
                   "用 Claude in Chrome 在用户的浏览器里完成下面的任务。需要用户本人决定的操作（提交表单、发消息或邮件、"
                   "付款、删除、修改账号设置、输入密码等）不要做，在回复里写清需要用户确认什么。"
                   "做完后在最后一条回复里写清结果，要的数据原样给出。\n\n任务：{task}")
BROWSER_MODEL, BROWSER_EFFORT, BROWSER_MODE = "claude-opus-5-5", "medium", "auto"     # the user's choice (11-20)
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


async def _until_done(hub: Hub, sid: str, seconds: float) -> bool:
    """Wait for the session's turn to end (True), or give up after `seconds` (False)."""
    start = time.time()
    seen_work = False
    while time.time() - start < seconds:
        working = hub.session_working(sid)
        if working is None:                      # not running (any more)
            return True
        if working:
            seen_work = True
        elif seen_work or time.time() - start > 120:
            return True
        await asyncio.sleep(1.5)
    return False


async def _reply_when_done(call: Call, child: str, wait: bool, label: str, stop_after: bool = False) -> str:
    """The child's reply once its turn ends: within the call (up to WAIT), or later as a message to the caller."""
    hub = call.hub

    async def reply() -> str:
        text = await _last_reply(call, child)
        if stop_after:
            with contextlib.suppress(ToolError):
                await call.api("POST", f"/v1/sessions/{child}/stop", {})
        _announce(hub, call.session_id)
        return text or "（它没有给出回复）"

    if wait and await _until_done(hub, child, WAIT):
        return await reply()

    async def later() -> None:
        await _until_done(hub, child, 6 * 3600)
        text = await reply()
        await hub.delivery.send(call.session_id, f"[{label} {child[:8]} 这一轮做完了，回复如下]\n\n{text}", [], str(uuidlib.uuid4()))
    _keep(asyncio.get_running_loop().create_task(later()))
    return (f"{label} {child[:8]} 还在做（可能在等用户批准）。做完后它的回复会作为一条消息发给你，不用反复查。")


async def _start_child(call: Call, harness: str, text: str, label: str, *, mode: str | None, model: str | None = None,
                       effort: str | None = None, directory: str | None = None, browser: bool = False) -> str:
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
            child = str((await hub.harnesses.create(harness, cwd, model=model, mode=mode)).session_id)
    except Exception as exc:  # noqa: BLE001 - why the harness could not start, for the agent
        raise ToolError(f"{harness} 没能启动：{exc}") from None
    hub.prefs.remember(child, startedBy=call.session_id, subagentOf=call.session_id, subagentTask=label[:200],
                       browser="1" if browser else None)
    with contextlib.suppress(ToolError):
        await call.api("POST", f"/v1/sessions/{child}/rename", {"title": label[:60]})
    if harness == "claude":
        await hub.delivery.send_new(proc, text, [], str(uuidlib.uuid4()))
    else:
        await hub.delivery.send(child, text, [], str(uuidlib.uuid4()))
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
    # As permissive as the caller: its mode's tier, in the target harness's own mode for that tier.
    caller = call.session_id
    if hub.harnesses.has(caller):
        summary = hub.harnesses.summary(caller)
    else:                                       # Claude Code: its mode now is in the running process's state
        summary = {**hub.session_summary(caller), "permissionMode": (hub.session_state(caller) or {}).get("permissionMode")}
    mode = hub._closest_mode(harness, hub._mode_tier(caller, summary))
    text = SUBAGENT_PREFACE.format(name=_harness_name(hub, caller), caller=caller[:8], task=task)
    child = await _start_child(call, harness, text, task, mode=mode, model=a.get("model") or None,
                               directory=a.get("directory"))
    reply = await _reply_when_done(call, child, a.get("wait", True) is not False, "子代理")
    return f"子代理 {child[:8]}（{_harness_name(hub, child)}）：\n\n{reply}\n\n（接着跟它说话用 subagent_send，agent_id={child[:8]}）"


async def subagent_send(call: Call, a: dict[str, Any]) -> str:
    hub = call.hub
    child = await call.resolve(a.get("agent_id"))
    if (hub.prefs.get(child) or {}).get("subagentOf") != call.session_id:
        raise ToolError("这不是你开的子代理（用 subagent_start 开的才行）")
    text = str(a.get("message") or "").strip()
    if not text:
        raise ToolError("message 不能为空")
    await hub.delivery.send(child, FOLLOW_UP.format(name=_harness_name(hub, call.session_id), caller=call.session_id[:8], text=text),
                            [], str(uuidlib.uuid4()))
    _announce(hub, call.session_id)
    reply = await _reply_when_done(call, child, a.get("wait", True) is not False, "子代理")
    return f"子代理 {child[:8]}（{_harness_name(hub, child)}）：\n\n{reply}"


async def browser_task(call: Call, a: dict[str, Any]) -> str:
    task = str(a.get("task") or "").strip()
    if not task:
        raise ToolError("task 不能为空")
    text = BROWSER_PREFACE.format(name=_harness_name(call.hub, call.session_id), caller=call.session_id[:8], task=task)
    child = await _start_child(call, "claude", text, "浏览器任务：" + task, mode=BROWSER_MODE, model=BROWSER_MODEL,
                               effort=BROWSER_EFFORT, browser=True)
    return await _reply_when_done(call, child, True, "浏览器任务", stop_after=True)


_SESSION_ID = {"type": "string", "description": "会话 ID（可以只写前 8 位）"}

TOOLS: list[Tool] = [
    Tool("subagent_start",
         "开一个子代理：用指定的工具（claude=Claude Code、codex、dsh=DeepSeek Harness、gemini=Gemini CLI、antigravity）"
         "开一个会话替你做一件事，等它这一轮做完把回复交给你。它显示在你的子代理列表里，权限和你一样宽松。"
         "之后用 subagent_send 接着跟它说话。最多等 8 分钟；超过或 wait=false 时先返回，做完后它的回复作为一条消息发给你。",
         {"task": {"type": "string", "description": "交给它的事，写清楚背景、要求和要交回什么"},
          "harness": {"type": "string", "enum": list(HARNESS_IDS), "description": "用哪个工具（默认 claude）"},
          "model": {"type": "string", "description": "模型（不写就用该工具的默认）"},
          "directory": {"type": "string", "description": "工作目录（默认是你的）"},
          "wait": {"type": "boolean", "description": "是否等它做完（默认 true）"}},
         subagent_start, required=("task",)),
    Tool("subagent_send", "给你用 subagent_start 开的子代理发一条消息（接着交流），等它这一轮做完把回复交给你（规则同 subagent_start）。",
         {"agent_id": {"type": "string", "description": "subagent_start 返回的 agent_id"},
          "message": {"type": "string"}, "wait": {"type": "boolean", "description": "是否等它做完（默认 true）"}},
         subagent_send, required=("agent_id", "message")),
    Tool("browser_task",
         "让一个开着 Claude in Chrome 的 Claude Code 会话，在用户自己的浏览器里（带用户的登录状态）替你完成任务，"
         "并把结果返回给你：查网页、读需要登录的页面、抄数据、截图等。它会出现在你的子代理列表里；"
         "提交、发送、付款、删除这类要用户本人决定的操作它不会做，会在结果里说明。"
         "最多等 8 分钟，超过的话先返回，做完后结果会作为一条消息发给你。",
         {"task": {"type": "string", "description": "要在浏览器里做的事，写清楚网址、要找什么、结果要什么格式"}},
         browser_task, required=("task",)),
    Tool("send_file",
         "把文件发给用户：图片、PDF、文档、表格、任何文件都行，会显示在用户的对话里（图片直接显示，其他文件可以点开查看或下载）。"
         "想让用户看一个文件时用它，不要只在回复里写路径或 file:// 链接。",
         {"path": {"type": "string", "description": "文件路径（相对路径按你的工作目录算）"},
          "paths": {"type": "array", "items": {"type": "string"}, "description": "一次发多个文件时用"},
          "caption": {"type": "string", "description": "附一句说明（可选）"}},
         send_file),
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
    Tool("session_list", "列出正在运行或设为常驻的会话（all 为真时列出最近的所有会话），包括用的是哪个工具。",
         {"all": {"type": "boolean"}}, session_list, read_only=True),
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
