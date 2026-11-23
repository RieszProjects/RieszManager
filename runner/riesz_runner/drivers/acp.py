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
import json
import logging
import os
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

from .base import BYPASS, Capabilities, Driver, Mode, ToolServer, most_permissive, assistant_item, command, interrupted_item, notice_item, tool_block, tool_result, user_item
from .jsonrpc import JsonRpcError, JsonRpcPeer, PeerClosed
from ..auto_resume import failure_from_text

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


def _normalize_questions(raw_questions: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for q in raw_questions or ():
        if not isinstance(q, dict):
            continue
        opts = []
        for o in q.get("options") or ():
            if isinstance(o, str):
                opts.append({"label": o})
            elif isinstance(o, dict):
                opts.append({"label": str(o.get("label") or o.get("title") or o.get("const") or ""),
                             **({"description": o["description"]} if o.get("description") else {})})
        out.append({
            "question": str(q.get("question") or ""),
            "multiSelect": bool(q.get("is_multi_select") or q.get("multiSelect")),
            "options": opts,
        })
    return out


def _elicitation_fields(schema: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert an ACP form elicitation JSON Schema into (AskUserQuestion items, field descriptors)."""
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    # Pair `<k>_other` string fields with their parent option field `<k>` (Antigravity's ask_question shape).
    paired_other: set[str] = set()
    for key, prop in props.items():
        if not isinstance(prop, dict):
            continue
        other_key = f"{key}_other"
        other_prop = props.get(other_key)
        has_choices = bool(prop.get("oneOf") or prop.get("enum") or
                           (prop.get("type") == "array" and isinstance(prop.get("items"), dict) and
                            (prop["items"].get("anyOf") or prop["items"].get("enum"))))
        if has_choices and isinstance(other_prop, dict) and other_prop.get("type") == "string":
            paired_other.add(other_key)

    questions: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    for key, prop in props.items():
        if key in paired_other or not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        title = str(prop.get("title") or prop.get("description") or key)
        other_key = f"{key}_other" if f"{key}_other" in paired_other else None
        options: list[dict[str, Any]] = []
        label_to_const: dict[str, Any] = {}
        multi = False
        if ptype == "array":
            multi = True
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            if isinstance(items.get("anyOf"), list):
                for item in items["anyOf"]:
                    if isinstance(item, dict):
                        label = str(item.get("title") or item.get("const") or "")
                        options.append({"label": label})
                        label_to_const[label] = item.get("const", label)
            elif isinstance(items.get("enum"), list):
                for val in items["enum"]:
                    label = str(val)
                    options.append({"label": label})
                    label_to_const[label] = val
        elif isinstance(prop.get("oneOf"), list):
            for item in prop["oneOf"]:
                if isinstance(item, dict):
                    label = str(item.get("title") or item.get("const") or "")
                    options.append({"label": label})
                    label_to_const[label] = item.get("const", label)
        elif isinstance(prop.get("enum"), list):
            for val in prop["enum"]:
                label = str(val)
                options.append({"label": label})
                label_to_const[label] = val
        elif ptype == "boolean":
            options = [{"label": "是"}, {"label": "否"}]
            label_to_const = {"是": True, "否": False, "true": True, "false": False}

        questions.append({"id": key, "question": title, "multiSelect": multi, "options": options})
        fields.append({"id": key, "question": title, "type": ptype, "multi": multi,
                       "otherKey": other_key, "labelToConst": label_to_const})
    return questions, fields


def _elicitation_content(fields: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, Any]:
    """Map AskUserQuestion answers back to ACP CreateElicitationResult.content."""
    content: dict[str, Any] = {}
    for field in fields:
        key = field["id"]
        given = answers.get(key)
        if given is None:
            given = answers.get(field["question"])
        picked = given if isinstance(given, list) else ([given] if given not in (None, "") else [])
        picked = [str(v) for v in picked if str(v).strip()]
        if not picked:
            continue
        label_map: dict[str, Any] = field["labelToConst"]
        other_key: str | None = field["otherKey"]
        if field["multi"]:
            selected: list[Any] = []
            customs: list[str] = []
            for item in picked:
                if item in label_map:
                    selected.append(label_map[item])
                else:
                    customs.append(item)
            if selected:
                content[key] = selected
            if customs:
                if other_key:
                    content[other_key] = ", ".join(customs)
                else:
                    content.setdefault(key, []).extend(customs)
        elif label_map:
            selected_val = None
            customs = []
            for item in picked:
                if item in label_map and selected_val is None:
                    selected_val = label_map[item]
                else:
                    customs.append(item)
            if selected_val is not None:
                content[key] = selected_val
            if customs:
                if other_key:
                    content[other_key] = ", ".join(customs)
                elif selected_val is None:
                    content[key] = ", ".join(customs)
        else:
            raw_text = ", ".join(picked)
            if field["type"] == "integer":
                try:
                    content[key] = int(raw_text)
                except ValueError:
                    content[key] = raw_text
            elif field["type"] == "number":
                try:
                    content[key] = float(raw_text)
                except ValueError:
                    content[key] = raw_text
            else:
                content[key] = raw_text
    return content


def tool_input(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(tool name, input) for an ACP tool call, shaped like the web app's tool cards."""
    kind = call.get("kind") or "other"
    title = str(call.get("title") or "")
    call_name = str(call.get("name") or "")
    raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else (
        call.get("args") if isinstance(call.get("args"), dict) else {})
    if call_name == "ask_question" or title == "Run ask_question":
        if isinstance(raw.get("questions"), list):
            return "AskUserQuestion", {"questions": _normalize_questions(raw["questions"])}
        return "AskUserQuestion", {"questions": []}
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
    return title or call_name or KIND_TOOLS.get(kind, "Tool"), {**raw} if raw else ({"title": title} if title else {})


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
    capabilities = Capabilities(queue=True, approve_for_session=True, user_questions=True, model_switch="next-turn",
                                effort_switch="none")
    default_modes: list[Mode] = []
    # Work the agent starts by itself (a background task finished, its own timer fired) counts as a turn of
    # its own until its updates have stopped for this long.
    autonomous_quiet = 30.0

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
        self._open: dict[str, float] = {}           # tool calls started and not finished: id -> when
        self._last_update = 0.0                     # when the last session/update came (monotonic)
        self._autonomous = False                    # working on its own, without a prompt of ours
        self._autonomous_cancel: asyncio.Event | None = None
        self._elicitation_answers: dict[str, dict[str, str]] = {}  # toolCallId -> {question: formatted answer}

    @property
    def pid(self) -> int | None:
        return self.peer.pid if self.peer else None

    def modes(self) -> list[Mode]:
        """The agent's modes, and Riesz Manager's BYPASS when none of them approves everything."""
        return self._modes if any(m.tier == "full" for m in self._modes) else [*self._modes, BYPASS]

    def _native(self, mode: str | None) -> str | None:
        """The agent's own mode for `mode`: BYPASS runs on the agent's most permissive one."""
        if mode == BYPASS.id:
            base = most_permissive(self._modes)
            return base.id if base else None
        return mode

    def _env(self) -> dict[str, str]:
        env = dict(self.env if self.env is not None else os.environ)
        env.pop("RUNNER_TOKEN", None)
        return env

    def mount_tools(self, server: ToolServer) -> list[dict[str, Any]]:
        """The ACP mcpServers entry (a stdio server) for session/new and session/load."""
        return [{"name": server.name, **server.stdio(), "env": []}]

    def _mcp_servers(self) -> list[dict[str, Any]]:
        return [entry for server in self.all_tools for entry in self.mount_tools(server)]

    # ---- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        self.peer = JsonRpcPeer(self.argv, cwd=self.cwd, env=self._env(), version=True,
                                on_notification=self._notification, on_request=self._request, on_exit=self._exited)
        await self.peer.start()
        self.alive = True
        self.started_at = time.time()
        try:
            init = await self.peer.request("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                                          "clientCapabilities": {"elicitation": {"form": {}}},
                                                          "clientInfo": {"name": "riesz-manager", "version": "1"}}, timeout=120)
            self.agent = init or {}
            if self.session_id and (self.agent.get("agentCapabilities") or {}).get("loadSession"):
                self._loading = True
                self.items.clear()
                try:
                    res = await self.peer.request("session/load", {"sessionId": self.session_id, "cwd": self.cwd, "mcpServers": self._mcp_servers()},
                                                  timeout=180)
                    # Antigravity answers before it has sent all of the history: the rest would land after the
                    # next message. The replay is over once the updates stop.
                    await self._settle()
                finally:
                    self._loading = False
                self._close_current()
                await self.reset()
            else:
                try:
                    res = await self.peer.request("session/new", {"cwd": self.cwd, "mcpServers": self._mcp_servers()}, timeout=180)
                except JsonRpcError as exc:
                    if not await self.authenticate(exc):
                        raise
                    res = await self.peer.request("session/new", {"cwd": self.cwd, "mcpServers": self._mcp_servers()}, timeout=180)
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
            self._modes = [Mode(m.get("id"), self.mode_name(m), self.mode_tier(m), self.mode_description(m))
                           for m in modes["availableModes"]]
        models = res.get("models") or {}
        if models.get("availableModels"):
            self._models = [{"value": m.get("modelId"), "displayName": m.get("name") or m.get("modelId"),
                             "description": m.get("description") or ""} for m in models["availableModels"]]
        current_mode, current_model = modes.get("currentModeId"), models.get("currentModelId")
        wanted = self._native(self.mode)
        if wanted and wanted != current_mode and any(m.id == wanted for m in self._modes):
            with contextlib.suppress(JsonRpcError):
                await self.peer.request("session/set_mode", {"sessionId": self.session_id, "modeId": wanted}, timeout=30)
                current_mode = wanted
        if self.mode == BYPASS.id and current_mode == wanted:
            current_mode = BYPASS.id
        if self.model and self.model != current_model:
            with contextlib.suppress(JsonRpcError):
                await self.peer.request("session/set_model", {"sessionId": self.session_id, "modelId": self.model}, timeout=30)
                current_model = self.model
        self.mode = current_mode or self.mode
        self.model = current_model or self.model

    def mode_name(self, mode: dict[str, Any]) -> str:
        return str(mode.get("name") or mode.get("id"))

    def mode_description(self, mode: dict[str, Any]) -> str:
        return str(mode.get("description") or "")

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
        self._open.clear()
        self.tasks.clear()                              # background calls end with the process
        self._autonomous = False
        during_turn = self.busy
        if self.busy:
            self.mark_unfinished_tools("exited")
            self.busy = False
        self.cancel_permissions()
        await self.flush()
        await self.emit({"ev": "exit", "code": code, "stderr": list(self.peer.stderr_tail)[-15:] if self.peer else [],
                         "duringTurn": during_turn})
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
        shown_images = [{"ref": f"file:{im['path']}", "mediaType": im.get("mime") or "image/png"}
                        for im in images if im.get("path")]
        self.items.put(user_item(f"a:u:{uuid}", text, uuid=uuid, images=shown_images, origin="user"))
        self._current = None
        self.turn_started()
        self._prompt_task = asyncio.get_running_loop().create_task(self._run_prompt(self._prompt(text, images)))

    async def _run_prompt(self, prompt: list[dict[str, Any]]) -> None:
        await self.flush()
        await self.emit_state()
        stop_reason, error, fail = None, None, None
        try:
            res = await self.peer.request("session/prompt", {"sessionId": self.session_id, "prompt": prompt}, timeout=None)
            stop_reason = (res or {}).get("stopReason")
        except JsonRpcError as exc:
            error = exc.message
            detail = exc.data if isinstance(exc.data, str) else json.dumps(exc.data, ensure_ascii=False) if exc.data else ""
            fail = failure_from_text(f"{exc.message} {detail}".strip())
        except PeerClosed:
            return                                      # _exited reports it
        self._close_current()
        if stop_reason == "cancelled":
            self._open.clear()
            self.mark_unfinished_tools("interrupted")
            self.items.put(interrupted_item(f"a:t:{self._turn}:end"))
            result = self.turn_finished("interrupted", stop_reason="interrupted")
        elif error:
            self._open.clear()
            self.mark_unfinished_tools("error")
            self.items.put(notice_item(f"a:t:{self._turn}:end", f"{self.harness} 这一轮没有完成：{error}", level="error"))
            result = self.turn_finished("error", error=True, stop_reason="error", failure=fail)
        else:
            if stop_reason in ("max_tokens", "max_turn_requests", "refusal"):
                self.items.put(notice_item(f"a:t:{self._turn}:end", f"这一轮停止了：{stop_reason}", level="warning"))
            self._to_background()
            result = self.turn_finished("success", stop_reason=stop_reason)
        await self.flush()
        await self.emit({"ev": "result", "result": result, "text": self.last_assistant_text()[:400]})
        if self._queue:
            text, images, uuid = self._queue.pop(0)
            self._start_turn(text, images, uuid)
        else:
            await self.emit_state()

    def _begin_autonomous(self) -> None:
        """The agent works without a prompt of ours (Antigravity: a background call finished, its own timer
        fired): shown as working, and a reload waits, until its updates stop."""
        self._autonomous = True
        self._autonomous_cancel = asyncio.Event()
        self._turn += 1
        self._close_current()
        self.turn_started()
        asyncio.get_running_loop().create_task(self._watch_autonomous())

    async def _watch_autonomous(self) -> None:
        await self.emit_state()
        cancel_ev = self._autonomous_cancel
        interrupted = False
        while self._autonomous and self.alive:
            if cancel_ev is not None and cancel_ev.is_set():
                interrupted = True
                break
            # Waiting for a user permission/question response is synchronous: never time out the turn while open.
            if self.permissions:
                self._last_update = time.monotonic()
            elif time.monotonic() - self._last_update >= self.autonomous_quiet:
                break
            wait_s = min(2.0, self.autonomous_quiet / 2)
            if cancel_ev is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(cancel_ev.wait(), timeout=wait_s)
            else:
                await asyncio.sleep(wait_s)
        if interrupted:
            # Drain the agent's immediate cancel-ack updates so they stay inside this turn instead of
            # spawning a second autonomous turn right after we mark the session idle.
            await self._settle(quiet=0.25, limit=1.5)
        if not self._autonomous:
            return                                      # a prompt of ours took over, or the process ended
        self._autonomous = False
        self._autonomous_cancel = None
        self._close_current()
        if interrupted:
            self._open.clear()
            self.mark_unfinished_tools("interrupted")
            self.items.put(interrupted_item(f"a:t:{self._turn}:end"))
            result = self.turn_finished("interrupted", stop_reason="interrupted")
        else:
            self._to_background()
            result = self.turn_finished("success", stop_reason="end_turn")
        await self.flush()
        await self.emit({"ev": "result", "result": result, "text": self.last_assistant_text()[:400]})
        if self._queue:
            text, images, uuid = self._queue.pop(0)
            self._start_turn(text, images, uuid)
        else:
            await self.emit_state()

    def wakeup_after(self, name: str, inp: dict[str, Any]) -> float | None:
        """Seconds after which the agent wakes itself up, when this call is its own timer (a harness's built-in
        schedule tool, which stays open until it fires): its background task says so."""
        return None

    def _to_background(self) -> None:
        """Calls still running when the turn ends go on in the background (Antigravity's long commands and
        subagents report later, and the agent carries on by itself): shown as background tasks, not as
        cut off, until they report."""
        for tid, started in list(self._open.items()):
            found = self.items.tool_block(tid)
            if found is None or found[1].get("result") is not None:
                self._open.pop(tid, None)
                continue
            block = found[1]
            wake = self.wakeup_after(block["name"], block["input"])
            summary = (f"{wake:.0f} 秒后自己接着做" if wake else
                       str(block["input"].get("command") or block["input"].get("title") or block["input"].get("description") or ""))
            self.tasks[tid] = {"id": tid, "description": (summary or block["name"])[:120], "type": "tool", "background": True,
                               "toolUseId": tid, "startedAt": started}

    async def interrupt(self) -> None:
        if self.peer is None or not self.busy:
            return
        self.cancel_permissions()
        if self._autonomous_cancel is not None:
            self._autonomous_cancel.set()
        with contextlib.suppress(PeerClosed):
            await self.peer.notify("session/cancel", {"sessionId": self.session_id})

    async def set_mode(self, mode: str) -> None:
        if mode not in {m.id for m in self.modes()}:
            raise ValueError(f"unknown mode {mode}")
        native = self._native(mode)
        if self.peer is not None and self.ready and native:
            await self.peer.request("session/set_mode", {"sessionId": self.session_id, "modeId": native}, timeout=30)
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
        if call.get("content") or call.get("kind") or call.get("rawInput") or call.get("args"):
            name, inp = tool_input({**call, "kind": self._kinds.get(tid, "other"), "title": call.get("title") or block["input"].get("title")})
            if inp and (name != "Tool" or block["name"] == "Tool"):
                block["name"], block["input"] = name, inp
        status = call.get("status")
        if status in ("completed", "failed"):
            answered = self._elicitation_answers.pop(tid, None)
            structured = {"answers": answered} if answered else None
            block["result"] = tool_result(tool_output(call), error=status == "failed", structured=structured)
            self._open.pop(tid, None)
            if self.tasks.pop(tid, None) is not None:
                asyncio.get_running_loop().create_task(self.emit_state())    # a background call finished
        elif not self._loading and block.get("result") is None:
            self._open.setdefault(tid, time.time())
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

    async def _settle(self, quiet: float = 0.5, limit: float = 15.0) -> None:
        """Wait until no session/update has come for `quiet` seconds (at most `limit`)."""
        start = time.monotonic()
        while time.monotonic() - start < limit:
            await asyncio.sleep(quiet / 2)
            if time.monotonic() - self._last_update >= quiet:
                return

    async def _notification(self, method: str, params: Any) -> None:
        if method != "session/update":
            return
        self._last_update = time.monotonic()
        u = (params or {}).get("update") or {}
        kind = u.get("sessionUpdate")
        if (kind in ("agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan")
                and not self._loading and self.ready and not self.busy):
            self._begin_autonomous()
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
            reported = u.get("currentModeId") or self.mode
            # Under BYPASS the agent runs in its own most permissive mode: that is still BYPASS.
            if not (self.mode == BYPASS.id and reported == self._native(BYPASS.id)):
                self.mode = reported
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
        if method == "elicitation/create":
            mode = p.get("mode") or "form"
            if mode != "form":
                raise JsonRpcError(-32601, f"unsupported elicitation mode: {mode}")
            schema = p.get("requestedSchema") if isinstance(p.get("requestedSchema"), dict) else {}
            questions, fields = _elicitation_fields(schema)
            if not questions:
                return {"action": "decline"}
            request_id = f"acp-{uuidlib.uuid4().hex[:12]}"
            tool_call_id = p.get("toolCallId")
            answer = await self.ask({
                "requestId": request_id,
                "toolName": "AskUserQuestion",
                "toolUseId": tool_call_id,
                "input": {"questions": questions},
                "title": p.get("message"),
                "question": True,
            })
            if answer.get("cancelled"):
                return {"action": "cancel"}
            if not answer.get("allow"):
                return {"action": "decline"}
            raw_answers = answer.get("answers") or {}
            content = _elicitation_content(fields, raw_answers)
            if tool_call_id:
                summary: dict[str, str] = {}
                for field in fields:
                    val = raw_answers.get(field["id"]) or raw_answers.get(field["question"])
                    if isinstance(val, list):
                        summary[field["question"]] = ", ".join(str(x) for x in val if str(x).strip())
                    elif val:
                        summary[field["question"]] = str(val)
                if summary:
                    self._elicitation_answers[str(tool_call_id)] = summary
            return {"action": "accept", "content": content}
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
