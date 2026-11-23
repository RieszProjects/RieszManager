"""What every harness driver offers the rest of the runner.

A driver runs one session of one harness (Codex, DeepSeek Harness, ...) through
that harness's own official programmatic interface. It turns what the harness
reports into the shapes the web app already shows for Claude Code sessions:

- conversation items (`ItemLog`): user and assistant messages, tool calls with
  their results, notices, interruptions;
- a state snapshot (`Driver.snapshot()`), shaped like the Claude Code process's;
- events passed to `sink(driver, event)`: {"ev": "items", "items": [...]},
  {"ev": "reset", "items": [...]}, {"ev": "state"}, {"ev": "permission", "request"},
  {"ev": "permission_resolved", "requestId"}, {"ev": "result", "result", "text"},
  {"ev": "notice", "level", "text"}, {"ev": "rate_limit", "info"}, {"ev": "exit", "code", "stderr", "duringTurn"}.

What a harness cannot do is declared in `capabilities`, so the web app can
leave it out rather than offer something that does nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

Sink = Callable[["Driver", dict[str, Any]], Awaitable[None]]

STREAM_EMIT_SECONDS = 0.12      # streamed text is passed on at most this often
MAX_TOOL_TEXT = 100_000         # tool output kept per call


@dataclass
class Capabilities:
    """What the web app may offer for sessions of this harness."""
    steer: bool = False          # a message sent while it works joins the current turn
    queue: bool = False          # a message sent while it works waits for the next turn
    interrupt: bool = True
    interrupt_restarts: bool = False   # interrupting ends and restarts the harness process
    images: bool = True
    approve_for_session: bool = False  # permission answers can cover the rest of the session
    user_questions: bool = False
    model_switch: str = "next-turn"    # "next-turn" | "restart" | "none"
    effort_switch: str = "next-turn"
    streaming_text: bool = True        # text arrives as it is written (else a step at a time)
    background_tasks: bool = False
    rate_limits: bool = False
    history: bool = True
    api_switch: bool = False           # a session can move to another model API (routes.py) and keep its history

    def as_dict(self) -> dict[str, Any]:
        return {_camel(k): v for k, v in self.__dict__.items()}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


# Slash commands every harness offers under one name, each run its own way. Every harness says how
# for each of them in HarnessSpec.commands (harnesses.py), with one of:
#   "/<command>"           its own slash command, sent as typed (checked to do this);
#   "offered:/<command>"   the same, when the harness lists that command (agents that report their commands);
#   "method:<name>"        a method of its driver, called with the arguments;
#   "none:<why>"           it has nothing that does this (the reason is shown when someone tries).
UNIVERSAL_COMMANDS: dict[str, str] = {
    "compact": "压缩对话上下文（用这个工具自己的压缩方式）",
}


def command(name: str, description: str, hint: str = "", builtin: bool = False) -> dict[str, Any]:
    """A slash command as the composer lists it."""
    return {"name": name, "description": description, "argumentHint": hint, "builtin": builtin}


def universal_commands() -> list[dict[str, Any]]:
    return [command(name, text, builtin=True) for name, text in UNIVERSAL_COMMANDS.items()]


@dataclass
class Mode:
    """A permission mode as the harness defines it, and its tier (modes.py) for moving between harnesses."""
    id: str
    name: str
    tier: str              # one of modes.TIERS: readonly, ask, edits, auto, full
    description: str = ""

    def __post_init__(self) -> None:
        from ..modes import TIERS
        if self.tier not in TIERS:
            raise ValueError(f"mode {self.id}: tier {self.tier!r} is not one of {TIERS}")


# Riesz Manager's own "approve everything" mode, for a harness without such a mode of its own: the
# harness runs in its most permissive mode, and every permission request it still makes is approved
# here (Driver.ask). Questions to the user still reach the user.
BYPASS = Mode("rz-bypass", "全部自动通过", "full", "Riesz Manager 自动批准它的所有权限请求；它向你提的问题照常显示")
TIER_ORDER = ("readonly", "ask", "edits", "auto", "full")


def most_permissive(modes: list[Mode]) -> Mode | None:
    """The harness's own mode that asks least (BYPASS itself left out)."""
    own = [m for m in modes if m.id != BYPASS.id]
    return max(own, key=lambda m: TIER_ORDER.index(m.tier), default=None)


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def tool_result(text: str = "", *, error: bool = False, structured: dict[str, Any] | None = None,
                images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    truncated = len(text) > MAX_TOOL_TEXT
    return {"text": text[:MAX_TOOL_TEXT], "truncated": truncated, "isError": error, "images": images or [],
            "structured": structured, "ts": iso()}


class ItemLog:
    """Conversation items in order, addressed by key; remembers which changed since the last flush."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._index: dict[str, int] = {}
        self._changed: dict[str, None] = {}
        self._seq = 0

    def get(self, key: str) -> dict[str, Any] | None:
        i = self._index.get(key)
        return self.items[i] if i is not None else None

    def put(self, item: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace by `item["key"]`; an item keeps its place and seq when replaced."""
        key = item["key"]
        i = self._index.get(key)
        if i is None:
            self._seq += 1
            item = {**item, "seq": self._seq}
            self._index[key] = len(self.items)
            self.items.append(item)
        else:
            item = {**item, "seq": self.items[i]["seq"]}
            self.items[i] = item
        self._changed[key] = None
        return item

    def touch(self, key: str) -> None:
        if key in self._index:
            self._changed[key] = None

    def take_changed(self) -> list[dict[str, Any]]:
        out = [self.items[self._index[k]] for k in self._changed if k in self._index]
        self._changed.clear()
        return out

    def clear(self) -> None:
        self.items.clear()
        self._index.clear()
        self._changed.clear()

    def tool_block(self, tool_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The (item, block) holding tool call `tool_id`, searching from the newest."""
        for item in reversed(self.items):
            for block in item.get("blocks") or ():
                if block.get("type") == "tool" and block.get("id") == tool_id:
                    return item, block
        return None


# ---- item constructors (the web app's Item shapes) -----------------------------------------

def user_item(key: str, text: str, *, ts: str | None = None, uuid: str | None = None,
              images: list[dict[str, Any]] | None = None, origin: str | None = None) -> dict[str, Any]:
    return {"kind": "user", "key": key, "seq": 0, "ts": ts or iso(), "uuid": uuid, "text": text,
            "images": images or [], "origin": origin}


def assistant_item(key: str, blocks: list[dict[str, Any]], *, ts: str | None = None, model: str | None = None,
                   msg_id: str | None = None, usage: dict[str, Any] | None = None,
                   stop_reason: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "assistant", "key": key, "seq": 0, "ts": ts or iso(), "uuid": None,
                            "msgId": msg_id, "model": model, "synthetic": False, "blocks": blocks,
                            "stopReason": stop_reason}
    if usage:
        item["usage"] = usage
    return item


def tool_block(tool_id: str, name: str, tool_input: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "tool", "id": tool_id, "name": name, "input": tool_input, "result": result}


def notice_item(key: str, text: str, *, level: str = "warning", subtype: str | None = None) -> dict[str, Any]:
    return {"kind": "notice", "key": key, "seq": 0, "ts": iso(), "uuid": None, "subtype": subtype, "level": level, "text": text}


def interrupted_item(key: str) -> dict[str, Any]:
    return {"kind": "interrupted", "key": key, "seq": 0, "ts": iso(), "uuid": None}


def command_item(key: str, name: str, args: str) -> dict[str, Any]:
    return {"kind": "command", "key": key, "seq": 0, "ts": iso(), "uuid": None, "name": name, "args": args}


def output_item(key: str, text: str, *, error: bool = False) -> dict[str, Any]:
    return {"kind": "output", "key": key, "seq": 0, "ts": iso(), "uuid": None, "text": text, "isError": error}


# ---- Riesz Manager's own tools --------------------------------------------------------------

@dataclass(frozen=True)
class ToolServer:
    """Riesz Manager's own tools (tools.py), offered to every harness as a stdio MCP server.

    Each driver mounts it through its harness's official MCP configuration (`Driver.mount_tools`),
    so the harness starts the server itself and lists the tools with its own.
    """
    name: str                       # the MCP server's name: tools show as mcp__<name>__<tool>
    command: str
    args: tuple[str, ...] = ()
    config_dir: str | None = None   # where a driver may write a config file its harness reads (dsh's --patch)

    def stdio(self) -> dict[str, Any]:
        """The usual stdio description: {"command", "args"}."""
        return {"command": self.command, "args": list(self.args)}


# ---- the driver -----------------------------------------------------------------------------

@dataclass
class PendingPermission:
    entry: dict[str, Any]
    future: asyncio.Future[dict[str, Any]] = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class Driver:
    """One session of one harness. Subclasses implement the harness's protocol."""

    harness = "?"
    capabilities = Capabilities()
    # Folders where the harness itself writes files for the user (plans, reports), "{id}" for the session id:
    # links to files there in its replies can be opened in the web app (server.py: open_file).
    artifact_dirs: tuple[str, ...] = ()

    def __init__(self, *, cwd: str, sink: Sink, session_id: str | None = None, model: str | None = None,
                 effort: str | None = None, mode: str | None = None, env: dict[str, str] | None = None,
                 tools: ToolServer | None = None, extra_tools: tuple[ToolServer, ...] = ()) -> None:
        self.cwd = cwd
        self.tools = tools              # Riesz Manager's tools, mounted in start() through mount_tools()
        self.extra_tools = tuple(extra_tools)
        self.sink = sink
        self.session_id = session_id
        self.model = model
        self.effort = effort
        self.mode = mode
        self.env = env
        self.items = ItemLog()
        self.alive = False
        self.ready = False
        self.busy = False
        self.status: str | None = None
        self.permissions: dict[str, PendingPermission] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.last_result: dict[str, Any] | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.context: dict[str, Any] | None = None       # {"used": tokens, "window": tokens}
        self.title: str | None = None
        self._flush_handle: asyncio.TimerHandle | None = None
        self._turn_started: float | None = None

    # ---- to be implemented ------------------------------------------------------------

    async def start(self) -> None:
        raise NotImplementedError

    async def send(self, text: str, images: list[dict[str, Any]] | None = None, uuid: str | None = None) -> None:
        """Send a message; `images` are {"path", "mime"}."""
        raise NotImplementedError

    async def interrupt(self) -> None:
        raise NotImplementedError

    async def set_model(self, model: str | None) -> None:
        self.model = model
        await self.emit_state()

    async def set_effort(self, effort: str | None) -> None:
        self.effort = effort
        await self.emit_state()

    async def set_mode(self, mode: str) -> None:
        self.mode = mode
        await self.emit_state()

    async def models(self) -> list[dict[str, Any]]:
        return []

    def modes(self) -> list[Mode]:
        return []

    # ---- slash commands ----------------------------------------------------------------

    def commands(self) -> list[dict[str, Any]]:
        """The composer's slash commands: the universal ones, then the harness's own."""
        universal = universal_commands()
        own = [c for c in self.native_commands() if c["name"] not in UNIVERSAL_COMMANDS]
        return universal + own

    def native_commands(self) -> list[dict[str, Any]]:
        """The harness's own slash commands (sent to it as they are typed)."""
        return []

    async def stop(self) -> None:
        raise NotImplementedError

    # ---- permissions --------------------------------------------------------------------

    def auto_approves(self, entry: dict[str, Any]) -> bool:
        """In a full-tier mode (the harness's own, or BYPASS) a permission request is approved without asking.
        Questions (AskUserQuestion, or entries marked "question") always go to the user."""
        if entry.get("question") or entry.get("toolName") == "AskUserQuestion":
            return False
        mode = next((m for m in self.modes() if m.id == self.mode), None)
        return mode is not None and mode.tier == "full"

    async def ask(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Show a permission request and wait for the answer: {"allow": bool, "scope": "once"|"session", "message", "answers"}."""
        if self.auto_approves(entry):
            log.info("%s %s: approved %s (mode %s)", self.harness, str(self.session_id)[:8], entry.get("toolName"), self.mode)
            return {"allow": True, "scope": "once", "message": None, "answers": {}}
        entry = {"suggestions": [], "ts": time.time(), **entry}
        pending = PendingPermission(entry)
        self.permissions[entry["requestId"]] = pending
        await self.sink(self, {"ev": "permission", "request": entry})
        await self.emit_state()
        try:
            return await pending.future
        finally:
            self.permissions.pop(entry["requestId"], None)
            await self.sink(self, {"ev": "permission_resolved", "requestId": entry["requestId"]})
            await self.emit_state()

    def answer_permission(self, request_id: str, *, allow: bool, scope: str = "once", message: str | None = None,
                          answers: dict[str, Any] | None = None) -> bool:
        pending = self.permissions.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result({"allow": allow, "scope": scope, "message": message, "answers": answers or {}})
        return True

    def cancel_permissions(self) -> None:
        for pending in list(self.permissions.values()):
            if not pending.future.done():
                pending.future.set_result({"allow": False, "scope": "once", "message": None, "answers": {}, "cancelled": True})

    # ---- state and events ---------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "hosted": True,
            "alive": self.alive,
            "ready": self.ready,
            "pid": self.pid,
            "busy": self.busy,
            "status": self.status,
            "model": self.model,
            "permissionMode": self.mode,
            "effort": self.effort,
            "queue": [],
            "permissions": [p.entry for p in self.permissions.values()],
            "tasks": list(self.tasks.values()),
            "partial": None,
            "lastResult": self.last_result,
            "startedAt": self.started_at,
            "exitCode": self.exit_code,
            "context": self.context,
            "capabilities": self.capabilities.as_dict(),
        }

    def has_pending_queue(self) -> bool:
        """Whether any user/system prompt is currently queued inside this driver."""
        return bool(self.snapshot().get("queue"))

    def has_queued(self, message_uuid: str) -> bool:
        """Whether `message_uuid` is still sitting in this driver's queue."""
        return any(q.get("uuid") == message_uuid for q in self.snapshot().get("queue") or ())

    @property
    def pid(self) -> int | None:
        return None

    @property
    def all_tools(self) -> list[ToolServer]:
        return [s for s in (self.tools, *self.extra_tools) if s is not None]

    def mount_tools(self, server: ToolServer) -> Any:
        """What start() adds to the harness's launch so it starts `server` as an MCP server (its own
        configuration format: arguments, a config entry, session parameters). Every driver must
        implement it unless its HarnessSpec says tools="none:<reason>"."""
        raise NotImplementedError

    async def emit(self, event: dict[str, Any]) -> None:
        await self.sink(self, event)

    async def emit_state(self) -> None:
        await self.sink(self, {"ev": "state", "state": self.snapshot()})

    async def flush(self) -> None:
        """Pass on the items that changed."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        changed = self.items.take_changed()
        if changed:
            await self.sink(self, {"ev": "items", "items": changed})

    def flush_soon(self) -> None:
        """Pass on changed items shortly (streamed text arrives many times a second)."""
        if self._flush_handle is None:
            loop = asyncio.get_running_loop()
            self._flush_handle = loop.call_later(STREAM_EMIT_SECONDS, lambda: asyncio.create_task(self.flush()))

    async def reset(self) -> None:
        """The whole conversation was (re)read: replace what the web app shows."""
        self.items.take_changed()
        await self.sink(self, {"ev": "reset", "items": list(self.items.items), "hasMore": False})

    def turn_started(self) -> None:
        self.busy = True
        self._turn_started = time.time()

    def turn_finished(self, subtype: str, *, error: bool = False, stop_reason: str | None = None,
                      failure: dict[str, Any] | None = None) -> dict[str, Any]:
        """`failure`: the turn failed for a reason that passes (auto_resume.failure()); it is picked up again later."""
        self.busy = False
        duration = (time.time() - self._turn_started) * 1000 if self._turn_started else None
        self._turn_started = None
        self.last_result = {"subtype": subtype, "isError": error, "durationMs": duration, "costUsd": None,
                            "numTurns": None, "stopReason": stop_reason, "ts": time.time()}
        if failure:
            self.last_result["failure"] = failure
        return self.last_result

    def last_assistant_text(self) -> str:
        for item in reversed(self.items.items):
            if item.get("kind") == "assistant":
                text = "".join(b.get("text", "") for b in item.get("blocks") or () if b.get("type") == "text")
                if text:
                    return text
            if item.get("kind") == "user":
                break
        return ""

    def mark_unfinished_tools(self, reason: str = "interrupted") -> None:
        """Tool calls still waiting for a result when a turn ends: they will not get one."""
        for item in self.items.items:
            changed = False
            for block in item.get("blocks") or ():
                if block.get("type") == "tool" and block.get("result") is None:
                    block["result"] = tool_result("", error=True, structured={"interrupted": True, "reason": reason})
                    changed = True
            if changed:
                self.items.touch(item["key"])

    async def close_permissions_and_flush(self) -> None:
        self.cancel_permissions()
        with contextlib.suppress(Exception):
            await self.flush()
