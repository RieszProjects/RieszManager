"""A driver for harnesses that speak the Agent Client Protocol (ACP) over stdio, such as `gemini --acp`.

ACP (agentclientprotocol.com) is JSON-RPC 2.0: the client sends `initialize`,
`session/new` or `session/load`, then `session/prompt` per message (answered
when the turn ends, with a stopReason) and `session/cancel` to interrupt. The
agent streams `session/update` notifications (message and thought chunks, tool
calls and their updates, plans, usage) and asks `session/request_permission`.

This client advertises no file system or terminal capability, so the agent uses
its own. A message sent while a turn runs waits here for the next turn: an ACP
agent takes one prompt at a time.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

from .base import Capabilities, Driver, Mode, assistant_item, command, interrupted_item, notice_item, tool_block, tool_result, user_item
from .jsonrpc import JsonRpcError, JsonRpcPeer, PeerClosed

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# ACP tool kinds → the tool names the web app knows.
KIND_TOOLS = {"execute": "Bash", "read": "Read", "edit": "Edit", "search": "Grep", "fetch": "WebFetch", "think": "Think",
              "delete": "Delete", "move": "Move"}
PLAN_STATUS = {"pending": "pending", "in_progress": "in_progress", "completed": "completed"}


def _text(block: Any) -> str:
    if isinstance(block, dict):
        if block.get("type") == "text":
            return str(block.get("text") or "")
        if block.get("type") == "resource_link":
            return str(block.get("uri") or "")
        if block.get("type") == "resource":
            return str((block.get("resource") or {}).get("text") or "")
    return ""


def tool_input(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(tool name, input) for an ACP tool call, shaped like the web app's tool cards."""
    kind = call.get("kind") or "other"
    title = str(call.get("title") or "")
    raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    locations = [loc.get("path") for loc in call.get("locations") or () if isinstance(loc, dict) and loc.get("path")]
    diffs = [c for c in call.get("content") or () if isinstance(c, dict) and c.get("type") == "diff"]
    if kind == "execute":
        return "Bash", {"command": raw.get("command") or title, "description": raw.get("description")}
    if kind == "edit" and diffs:
        d = diffs[0]
        if not d.get("oldText"):
            return "Write", {"file_path": d.get("path"), "content": d.get("newText") or ""}
        return "Edit", {"file_path": d.get("path"), "old_string": d.get("oldText") or "", "new_string": d.get("newText") or ""}
    if kind in ("read", "edit", "delete", "move") and locations:
        return KIND_TOOLS[kind], {"file_path": locations[0], "title": title, **raw}
    if kind == "search":
        return "Grep", {"pattern": raw.get("pattern") or title, **raw}
    if kind == "fetch":
        return "WebFetch", {"url": raw.get("url") or title, **raw}
    return title or KIND_TOOLS.get(kind, "Tool"), {**raw} if raw else ({"title": title} if title else {})


def tool_output(call: dict[str, Any]) -> str:
    parts = []
    for c in call.get("content") or ():
        if isinstance(c, dict) and c.get("type") == "content":
            parts.append(_text(c.get("content")))
    if not parts and call.get("rawOutput") is not None:
        raw = call["rawOutput"]
        parts.append(raw if isinstance(raw, str) else str(raw.get("output") or raw.get("result") or "") if isinstance(raw, dict) else str(raw))
    return "\n".join(p for p in parts if p)


class AcpDriver(Driver):
    """One session of an ACP agent. Subclasses give the command (`argv`) and their modes."""

    harness = "acp"
    capabilities = Capabilities(queue=True, approve_for_session=True, user_questions=False, model_switch="next-turn",
                                effort_switch="none")
    default_modes: list[Mode] = []

    def __init__(self, *, argv: list[str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.argv = argv or []
        self.peer: JsonRpcPeer | None = None
        self.agent: dict[str, Any] = {}
        self._modes: list[Mode] = list(self.default_modes)
        self._models: list[dict[str, Any]] = []
        self._queue: list[tuple[str, list[dict[str, Any]], str]] = []
        self._turn = 0
        self._current: str | None = None        # key of the assistant item being written
        self._loading = False                    # history is being replayed by session/load
        self._prompt_task: asyncio.Task[Any] | None = None
        self._kinds: dict[str, str] = {}          # tool call id -> its ACP kind (updates may leave it out)
        self._commands: list[dict[str, Any]] = []   # the agent's own slash commands (available_commands_update)

    @property
    def pid(self) -> int | None:
        return self.peer.pid if self.peer else None

    def modes(self) -> list[Mode]:
        return self._modes

    def _env(self) -> dict[str, str]:
        env = dict(self.env if self.env is not None else os.environ)
        env.pop("RUNNER_TOKEN", None)
        return env

    # ---- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        self.peer = JsonRpcPeer(self.argv, cwd=self.cwd, env=self._env(), version=True,
                                on_notification=self._notification, on_request=self._request, on_exit=self._exited)
        await self.peer.start()
        self.alive = True
        self.started_at = time.time()
        try:
            init = await self.peer.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {},
                                                          "clientInfo": {"name": "riesz-manager", "version": "1"}}, timeout=120)
            self.agent = init or {}
            if self.session_id and (self.agent.get("agentCapabilities") or {}).get("loadSession"):
                self._loading = True
                self.items.clear()
                try:
                    res = await self.peer.request("session/load", {"sessionId": self.session_id, "cwd": self.cwd, "mcpServers": []},
                                                  timeout=180)
                finally:
                    self._loading = False
                self._close_current()
                await self.reset()
            else:
                try:
                    res = await self.peer.request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=180)
                except JsonRpcError as exc:
                    if not await self.authenticate(exc):
                        raise
                    res = await self.peer.request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=180)
                self.session_id = res.get("sessionId") or self.session_id
            await self._adopt(res or {})
        except Exception:
            await self.stop()
            raise
        self.ready = True
        await self.emit_state()

    async def _adopt(self, res: dict[str, Any]) -> None:
        """Modes and models from session/new or session/load; then the ones this session should use."""
        modes = res.get("modes") or {}
        if modes.get("availableModes"):
            self._modes = [Mode(m.get("id"), self.mode_name(m), self.mode_tier(m), m.get("description") or "")
                           for m in modes["availableModes"]]
        models = res.get("models") or {}
        if models.get("availableModels"):
            self._models = [{"value": m.get("modelId"), "displayName": m.get("name") or m.get("modelId"),
                             "description": m.get("description") or ""} for m in models["availableModels"]]
        current_mode, current_model = modes.get("currentModeId"), models.get("currentModelId")
        if self.mode and self.mode != current_mode and any(m.id == self.mode for m in self._modes):
            with contextlib.suppress(JsonRpcError):
                await self.peer.request("session/set_mode", {"sessionId": self.session_id, "modeId": self.mode}, timeout=30)
                current_mode = self.mode
        if self.model and self.model != current_model:
            with contextlib.suppress(JsonRpcError):
                await self.peer.request("session/set_model", {"sessionId": self.session_id, "modelId": self.model}, timeout=30)
                current_model = self.model
        self.mode = current_mode or self.mode
        self.model = current_model or self.model

    def mode_name(self, mode: dict[str, Any]) -> str:
        return str(mode.get("name") or mode.get("id"))

    def mode_tier(self, mode: dict[str, Any]) -> str:
        """The tier (modes.py) of a mode the agent reported; its known modes first, then a guess from the id."""
        known = {m.id: m.tier for m in self.default_modes}
        mode_id = str(mode.get("id") or "")
        if mode_id in known:
            return known[mode_id]
        lowered = mode_id.lower()
        for words, tier in ((("plan", "read"), "readonly"), (("yolo", "bypass", "full", "danger"), "full"),
                            (("edit",), "edits"), (("auto",), "auto")):
            if any(w in lowered for w in words):
                return tier
        return "ask"

    async def authenticate(self, error: JsonRpcError) -> bool:
        """session/new was refused: sign in (`authenticate`) if this agent knows how, then it is tried once more."""
        return False

    async def stop(self) -> None:
        self.cancel_permissions()
        if self.peer is not None:
            await self.peer.stop()

    async def _exited(self, code: int | None) -> None:
        self.alive = False
        self.ready = False
        self.exit_code = code
        if self.busy:
            self.mark_unfinished_tools("exited")
            self.busy = False
        self.cancel_permissions()
        await self.flush()
        await self.emit({"ev": "exit", "code": code, "stderr": list(self.peer.stderr_tail)[-15:] if self.peer else []})
        await self.emit_state()

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap["queue"] = [{"uuid": uid, "preview": text[:300], "state": "queued", "ts": None} for text, _, uid in self._queue]
        return snap

    # ---- what the user does -------------------------------------------------------------

    def _prompt(self, text: str, images: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for im in images or ():
            data = Path(im["path"]).read_bytes()
            blocks.append({"type": "image", "data": base64.b64encode(data).decode(), "mimeType": im.get("mime") or "image/png"})
        return blocks

    async def send(self, text: str, images: list[dict[str, Any]] | None = None, uuid: str | None = None) -> None:
        assert self.peer is not None
        uuid = uuid or str(uuidlib.uuid4())
        if self.busy:
            self._queue.append((text, images or [], uuid))
            await self.emit_state()
            return
        self._start_turn(text, images or [], uuid)

    def _start_turn(self, text: str, images: list[dict[str, Any]], uuid: str) -> None:
        self._turn += 1
        self.items.put(user_item(f"a:u:{uuid}", text, uuid=uuid, origin="user"))
        self._current = None
        self.turn_started()
        self._prompt_task = asyncio.get_running_loop().create_task(self._run_prompt(self._prompt(text, images)))

    async def _run_prompt(self, prompt: list[dict[str, Any]]) -> None:
        await self.flush()
        await self.emit_state()
        stop_reason, error = None, None
        try:
            res = await self.peer.request("session/prompt", {"sessionId": self.session_id, "prompt": prompt}, timeout=None)
            stop_reason = (res or {}).get("stopReason")
        except JsonRpcError as exc:
            error = exc.message
        except PeerClosed:
            return                                      # _exited reports it
        self._close_current()
        if stop_reason == "cancelled":
            self.mark_unfinished_tools("interrupted")
            self.items.put(interrupted_item(f"a:t:{self._turn}:end"))
            result = self.turn_finished("interrupted", stop_reason="interrupted")
        elif error:
            self.mark_unfinished_tools("error")
            self.items.put(notice_item(f"a:t:{self._turn}:end", f"{self.harness} 这一轮没有完成：{error}", level="error"))
            result = self.turn_finished("error", error=True, stop_reason="error")
        else:
            if stop_reason in ("max_tokens", "max_turn_requests", "refusal"):
                self.items.put(notice_item(f"a:t:{self._turn}:end", f"这一轮停止了：{stop_reason}", level="warning"))
            result = self.turn_finished("success", stop_reason=stop_reason)
        await self.flush()
        await self.emit({"ev": "result", "result": result, "text": self.last_assistant_text()[:400]})
        if self._queue:
            text, images, uuid = self._queue.pop(0)
            self._start_turn(text, images, uuid)
        else:
            await self.emit_state()

    async def interrupt(self) -> None:
        if self.peer is None or not self.busy:
            return
        self.cancel_permissions()
        with contextlib.suppress(PeerClosed):
            await self.peer.notify("session/cancel", {"sessionId": self.session_id})

    async def set_mode(self, mode: str) -> None:
        if mode not in {m.id for m in self._modes}:
            raise ValueError(f"unknown mode {mode}")
        if self.peer is not None and self.ready:
            await self.peer.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode}, timeout=30)
        await super().set_mode(mode)

    async def set_model(self, model: str | None) -> None:
        if model and self.peer is not None and self.ready:
            await self.peer.request("session/set_model", {"sessionId": self.session_id, "modelId": model}, timeout=30)
        await super().set_model(model)

    async def models(self) -> list[dict[str, Any]]:
        return list(self._models)

    def native_commands(self) -> list[dict[str, Any]]:
        return [command(str(c.get("name")), str(c.get("description") or ""), str((c.get("input") or {}).get("hint") or ""))
                for c in self._commands if c.get("name")]



    # ---- what the agent reports ---------------------------------------------------------

    def _assistant(self) -> dict[str, Any]:
        if self._current is None or self.items.get(self._current) is None:
            n = len(self.items.items)
            self._current = f"a:a:{self._turn}:{n}"
            self.items.put(assistant_item(self._current, [], model=self.model))
        return self.items.get(self._current)  # type: ignore[return-value]

    def _close_current(self) -> None:
        self._current = None

    def _chunk(self, kind: str, text: str) -> None:
        item = self._assistant()
        blocks = item["blocks"]
        if not blocks or blocks[-1].get("type") != kind:
            blocks.append({"type": kind, "text": "", **({"redacted": False} if kind == "thinking" else {})})
        blocks[-1]["text"] += text
        self.items.touch(item["key"])

    def _tool(self, call: dict[str, Any]) -> None:
        tid = str(call.get("toolCallId") or uuidlib.uuid4().hex)
        found = self.items.tool_block(tid)
        if found is None:
            name, inp = tool_input(call)
            item = self._assistant()
            item["blocks"].append(tool_block(tid, name, inp))
            found = (item, item["blocks"][-1])
        item, block = found
        if call.get("kind"):
            self._kinds[tid] = call["kind"]
        if call.get("content") or call.get("kind") or call.get("rawInput"):
            name, inp = tool_input({**call, "kind": self._kinds.get(tid, "other"), "title": call.get("title") or block["input"].get("title")})
            if inp and (name != "Tool" or block["name"] == "Tool"):
                block["name"], block["input"] = name, inp
        status = call.get("status")
        if status in ("completed", "failed"):
            block["result"] = tool_result(tool_output(call), error=status == "failed")
        self.items.touch(item["key"])

    def _user_chunk(self, content: Any) -> None:
        """History replay (session/load): the user's messages."""
        self._turn += 1
        self._close_current()
        self.items.put(user_item(f"a:u:h{self._turn}", _text(content), origin="user"))

    def _plan(self, entries: list[dict[str, Any]]) -> None:
        todos = [{"content": e.get("content", ""), "activeForm": e.get("content", ""), "status": PLAN_STATUS.get(e.get("status"), "pending")}
                 for e in entries]
        key = f"a:plan:{self._turn}"
        self.items.put(assistant_item(key, [tool_block(key, "TodoWrite", {"todos": todos}, tool_result(""))]))
        self._close_current()

    async def _notification(self, method: str, params: Any) -> None:
        if method != "session/update":
            return
        u = (params or {}).get("update") or {}
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._chunk("text", _text(u.get("content")))
        elif kind == "agent_thought_chunk":
            self._chunk("thinking", _text(u.get("content")))
        elif kind == "user_message_chunk":
            if self._loading:
                self._user_chunk(u.get("content"))
        elif kind in ("tool_call", "tool_call_update"):
            self._tool(u)
        elif kind == "plan":
            self._plan(u.get("entries") or [])
        elif kind == "usage_update":
            self.context = {"used": u.get("used"), "window": u.get("size")}
            if not self._loading:
                await self.emit_state()
            return
        elif kind == "current_mode_update":
            self.mode = u.get("currentModeId") or self.mode
            if not self._loading:
                await self.emit_state()
            return
        elif kind == "session_info_update":
            self.title = u.get("title") or self.title
            return
        elif kind == "available_commands_update":
            self._commands = [c for c in u.get("availableCommands") or () if isinstance(c, dict)]
            return
        else:
            return
        if not self._loading:
            self.flush_soon()

    async def _request(self, method: str, params: Any) -> Any:
        p = params or {}
        if method == "session/request_permission":
            call = p.get("toolCall") or {}
            self._tool({**call, "status": None})
            name, inp = tool_input(call)
            options = [o for o in p.get("options") or () if isinstance(o, dict)]
            request_id = f"acp-{uuidlib.uuid4().hex[:12]}"
            entry = {"requestId": request_id, "toolName": name, "toolUseId": call.get("toolCallId"), "input": inp,
                     "title": call.get("title")}
            answer = await self.ask(entry)
            if answer.get("cancelled"):
                return {"outcome": {"outcome": "cancelled"}}
            wanted = ("allow_always", "allow_once") if answer.get("allow") and answer.get("scope") == "session" \
                else ("allow_once", "allow_always") if answer.get("allow") else ("reject_once", "reject_always")
            for kind in wanted:
                option = next((o for o in options if o.get("kind") == kind), None)
                if option is not None:
                    return {"outcome": {"outcome": "selected", "optionId": option.get("optionId")}}
            return {"outcome": {"outcome": "cancelled"}}
        raise JsonRpcError(-32601, f"{method} is not supported by this client")
