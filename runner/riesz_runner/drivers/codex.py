"""OpenAI Codex, driven through `codex app-server` (its official JSON-RPC interface for rich clients).

One driver is one Codex thread. Codex reports items (user message, agent
message, reasoning, command execution, file change, MCP call, ...) with
started/completed notifications and text deltas in between; each Codex item
becomes one conversation item here, keyed by its id.

Permission modes are Codex's own presets: an approval policy plus a sandbox.
Model, effort and mode apply per turn, so a change takes effect with the next
message. A message sent while Codex works joins the running turn (`turn/steer`).

The protocol is version-specific; `codex app-server generate-ts` prints the
types of the installed version.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import shlex
import time
import uuid as uuidlib
from typing import Any

from .base import (
    Capabilities,
    Driver,
    Mode,
    assistant_item,
    iso,
    interrupted_item,
    notice_item,
    tool_block,
    tool_result,
    user_item,
)
from .jsonrpc import JsonRpcError, JsonRpcPeer, PeerClosed

log = logging.getLogger(__name__)

CLIENT_INFO = {"name": "riesz-manager", "title": "Riesz Manager", "version": "1"}

MODES = [
    Mode("read-only", "只读", "只能读文件；改文件或联网要先问你"),
    Mode("auto", "自动", "可以改工作区里的文件、运行命令；越出工作区或要联网时先问你"),
    Mode("full-access", "完全访问", "不受沙箱限制，也不再询问"),
]
DEFAULT_MODE = "auto"

# mode -> (approvalPolicy, sandbox for thread/start, sandboxPolicy for turn/start)
_POLICY: dict[str, tuple[str, str, dict[str, Any]]] = {
    "read-only": ("on-request", "read-only", {"type": "readOnly", "networkAccess": False}),
    "auto": ("on-request", "workspace-write", {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                                                "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}),
    "full-access": ("never", "danger-full-access", {"type": "dangerFullAccess"}),
}

_PLAN_STATUS = {"pending": "pending", "inProgress": "in_progress", "in_progress": "in_progress", "completed": "completed"}


def unwrap_command(command: str) -> str:
    """`/bin/bash -lc "cmd"` -> `cmd`: Codex runs every command through a login shell."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) == 3 and os.path.basename(parts[0]) in ("bash", "sh", "zsh") and parts[1] in ("-lc", "-c"):
        return parts[2]
    return command


def _text_of_input(content: list[dict[str, Any]]) -> str:
    return "\n".join(c.get("text", "") for c in content or () if c.get("type") == "text")


def _mcp_text(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    parts = []
    for c in result.get("content") or ():
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif c is not None:
            parts.append(json.dumps(c, ensure_ascii=False))
    return "\n".join(parts)


def map_item(item: dict[str, Any], *, model: str | None = None, ts: str | None = None) -> dict[str, Any] | None:
    """A Codex ThreadItem as a conversation item (None for kinds that show nothing)."""
    kind = item.get("type")
    key = f"c:{item.get('id')}"
    ts = ts or (iso(item["startedAtMs"] / 1000) if item.get("startedAtMs") else None)
    if kind == "userMessage":
        content = item.get("content") or []
        images = [{"ref": f"file:{c['path']}", "mediaType": mimetypes.guess_type(c["path"])[0] or "image/png"}
                  for c in content if isinstance(c, dict) and c.get("type") == "localImage" and c.get("path")]
        return user_item(key, _text_of_input(content), ts=ts, uuid=item.get("clientId"), origin="user", images=images)
    if kind == "agentMessage":
        return assistant_item(key, [{"type": "text", "text": item.get("text") or ""}], ts=ts, model=model, msg_id=item.get("id"))
    if kind == "plan":
        return assistant_item(key, [{"type": "text", "text": item.get("text") or ""}], ts=ts, model=model, msg_id=item.get("id"))
    if kind == "reasoning":
        text = "\n\n".join(item.get("summary") or []) or "\n\n".join(item.get("content") or [])
        return assistant_item(key, [{"type": "thinking", "text": text, "redacted": not text}], ts=ts, model=model)
    if kind == "commandExecution":
        block = tool_block(str(item.get("id")), "Bash", {"command": unwrap_command(item.get("command") or ""), "cwd": item.get("cwd")})
        block["result"] = _command_result(item)
        return assistant_item(key, [block], ts=ts, model=model)
    if kind == "fileChange":
        block = tool_block(str(item.get("id")), "ApplyPatch", {"changes": item.get("changes") or []})
        status = item.get("status")
        if status in ("completed", "failed", "declined"):
            block["result"] = tool_result("", error=status != "completed", structured={"status": status})
        return assistant_item(key, [block], ts=ts, model=model)
    if kind == "mcpToolCall":
        block = tool_block(str(item.get("id")), f"mcp__{item.get('server')}__{item.get('tool')}",
                           item.get("arguments") if isinstance(item.get("arguments"), dict) else {"arguments": item.get("arguments")})
        status = item.get("status")
        if status not in (None, "inProgress"):
            error = item.get("error")
            block["result"] = tool_result(_mcp_text(item.get("result")) or (error or {}).get("message", ""), error=bool(error) or status == "failed")
        return assistant_item(key, [block], ts=ts, model=model)
    if kind == "webSearch":
        block = tool_block(str(item.get("id")), "WebSearch", {"query": item.get("query") or ""})
        if item.get("completedAtMs") or item.get("action"):
            block["result"] = tool_result("")
        return assistant_item(key, [block], ts=ts, model=model)
    if kind == "imageView":
        block = tool_block(str(item.get("id")), "Read", {"file_path": item.get("path")}, tool_result(""))
        return assistant_item(key, [block], ts=ts, model=model)
    if kind == "contextCompaction":
        return {"kind": "compact", "key": key, "seq": 0, "ts": ts or iso(), "uuid": None, "summary": None}
    if kind in ("enteredReviewMode", "exitedReviewMode"):
        return notice_item(key, item.get("review") or "", level="info", subtype=kind)
    if kind in ("hookPrompt", "functionCallOutput"):
        return None
    # Other tool-like items (dynamic tools, sub-agents, ...): shown with their raw fields.
    fields = {k: v for k, v in item.items() if k not in ("type", "id", "startedAtMs", "completedAtMs")}
    block = tool_block(str(item.get("id")), str(kind), fields)
    if item.get("status") not in (None, "inProgress", "running"):
        block["result"] = tool_result("", error=item.get("status") == "failed")
    return assistant_item(key, [block], ts=ts, model=model)


def _command_result(item: dict[str, Any]) -> dict[str, Any] | None:
    status = item.get("status")
    if status in (None, "inProgress"):
        return None
    exit_code = item.get("exitCode")
    return tool_result(item.get("aggregatedOutput") or "", error=status != "completed" or bool(exit_code),
                       structured={"status": status, "exitCode": exit_code, "durationMs": item.get("durationMs")})


def plan_item(key: str, plan: list[dict[str, Any]], explanation: str | None) -> dict[str, Any]:
    todos = [{"content": s.get("step", ""), "activeForm": s.get("step", ""), "status": _PLAN_STATUS.get(s.get("status"), "pending")}
             for s in plan]
    block = tool_block(key, "TodoWrite", {"todos": todos, "explanation": explanation}, tool_result(""))
    return assistant_item(key, [block])


class CodexDriver(Driver):
    harness = "codex"
    capabilities = Capabilities(steer=True, approve_for_session=True, user_questions=True, rate_limits=True)

    def __init__(self, *, codex: str = "codex", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.codex = codex
        self.mode = self.mode or DEFAULT_MODE
        self.peer: JsonRpcPeer | None = None
        self.turn_id: str | None = None
        self.rate_limits: dict[str, Any] | None = None
        self._plans: dict[str, str] = {}        # turn id -> plan item key

    @property
    def pid(self) -> int | None:
        return self.peer.pid if self.peer else None

    def modes(self) -> list[Mode]:
        return MODES

    # ---- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        env = dict(self.env if self.env is not None else os.environ)
        env.pop("RUNNER_TOKEN", None)
        self.peer = JsonRpcPeer([self.codex, "app-server"], cwd=self.cwd, env=env, version=False,
                                on_notification=self._notification, on_request=self._request, on_exit=self._exited)
        await self.peer.start()
        self.alive = True
        self.started_at = time.time()
        try:
            await self.peer.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": None}, timeout=60)
            await self.peer.notify("initialized")
            approval, sandbox, _ = _POLICY[self.mode]
            params: dict[str, Any] = {"cwd": self.cwd, "approvalPolicy": approval, "sandbox": sandbox}
            if self.model:
                params["model"] = self.model
            if self.session_id:
                res = await self.peer.request("thread/resume", {"threadId": self.session_id, "excludeTurns": True, **params}, timeout=60)
                self._adopt(res)
                await self.load_history()
            else:
                res = await self.peer.request("thread/start", params, timeout=60)
                self._adopt(res)
        except Exception:
            await self.stop()
            raise
        self.ready = True
        await self.emit_state()

    def _adopt(self, res: dict[str, Any]) -> None:
        thread = res.get("thread") or {}
        self.session_id = thread.get("id") or self.session_id
        self.model = self.model or res.get("model") or thread.get("model")
        self.effort = self.effort or res.get("reasoningEffort") or thread.get("reasoningEffort")
        self.title = thread.get("name") or thread.get("preview") or self.title

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

    # ---- what the user does -------------------------------------------------------------

    def _input(self, text: str, images: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [{"type": "localImage", "path": im["path"]} for im in images or () if im.get("path")]
        if text:
            blocks.append({"type": "text", "text": text, "text_elements": []})
        return blocks

    async def send(self, text: str, images: list[dict[str, Any]] | None = None, uuid: str | None = None) -> None:
        assert self.peer is not None
        uuid = uuid or str(uuidlib.uuid4())
        user_input = self._input(text, images)
        if self.busy and self.turn_id:
            with contextlib.suppress(JsonRpcError):
                await self.peer.request("turn/steer", {"threadId": self.session_id, "input": user_input,
                                                       "expectedTurnId": self.turn_id, "clientUserMessageId": uuid})
                return
            # The turn ended meanwhile: start a new one below.
        approval, _, sandbox_policy = _POLICY[self.mode]
        params: dict[str, Any] = {"threadId": self.session_id, "input": user_input, "clientUserMessageId": uuid,
                                  "approvalPolicy": approval, "sandboxPolicy": sandbox_policy}
        if self.model:
            params["model"] = self.model
        if self.effort:
            params["effort"] = self.effort
        self.turn_started()
        await self.emit_state()
        try:
            res = await self.peer.request("turn/start", params, timeout=60)
        except Exception:
            self.busy = False
            await self.emit_state()
            raise
        self.turn_id = (res.get("turn") or {}).get("id") or self.turn_id

    async def interrupt(self) -> None:
        if self.peer is None or not self.busy or not self.turn_id:
            return
        self.cancel_permissions()
        with contextlib.suppress(JsonRpcError, PeerClosed):
            await self.peer.request("turn/interrupt", {"threadId": self.session_id, "turnId": self.turn_id}, timeout=30)

    async def set_mode(self, mode: str) -> None:
        if mode not in _POLICY:
            raise ValueError(f"unknown mode {mode}")
        await super().set_mode(mode)

    async def models(self) -> list[dict[str, Any]]:
        assert self.peer is not None
        res = await self.peer.request("model/list", {}, timeout=30)
        out = []
        for m in res.get("data") or ():
            if m.get("hidden"):
                continue
            out.append({"value": m.get("model") or m.get("id"), "displayName": m.get("displayName") or m.get("model"),
                        "description": m.get("description") or "", "isDefault": bool(m.get("isDefault")),
                        "supportedEffortLevels": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or ()],
                        "defaultEffort": m.get("defaultReasoningEffort")})
        return out

    async def read_rate_limits(self) -> dict[str, Any] | None:
        assert self.peer is not None
        res = await self.peer.request("account/rateLimits/read", None, timeout=30)
        self.rate_limits = res.get("rateLimits")
        return self.rate_limits

    async def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        assert self.peer is not None
        res = await self.peer.request("thread/list", {"cwd": self.cwd, "limit": limit}, timeout=30)
        return res.get("data") or []

    # ---- history ------------------------------------------------------------------------

    async def load_history(self) -> None:
        """Rebuild the conversation from Codex's record of the thread, oldest turn first."""
        assert self.peer is not None
        turns: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"threadId": self.session_id, "itemsView": "full", "sortDirection": "asc", "limit": 50}
            if cursor:
                params["cursor"] = cursor
            res = await self.peer.request("thread/turns/list", params, timeout=60)
            turns.extend(res.get("data") or [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.items.clear()
        for turn in turns:
            self.add_turn(turn)
        await self.reset()

    def add_turn(self, turn: dict[str, Any]) -> None:
        for item in turn.get("items") or ():
            mapped = map_item(item, model=self.model)
            if mapped:
                self.items.put(mapped)
        self._turn_end_items(turn)

    def _turn_end_items(self, turn: dict[str, Any]) -> None:
        status = turn.get("status")
        if status == "interrupted":
            self.items.put(interrupted_item(f"c:{turn.get('id')}:interrupted"))
        elif status == "failed":
            message = (turn.get("error") or {}).get("message") or "Codex 这一轮失败了"
            self.items.put(notice_item(f"c:{turn.get('id')}:error", message, level="error"))

    # ---- what Codex reports -------------------------------------------------------------

    async def _notification(self, method: str, params: Any) -> None:
        p = params or {}
        if p.get("threadId") not in (None, self.session_id):
            return                                          # another thread (sub-agents): not shown here yet
        if method == "turn/started":
            self.turn_id = (p.get("turn") or {}).get("id") or self.turn_id
            if not self.busy:
                self.turn_started()
            await self.emit_state()
        elif method in ("item/started", "item/completed"):
            mapped = map_item(p.get("item") or {}, model=self.model)
            if mapped:
                old = self.items.get(mapped["key"])
                if old and method == "item/completed" and mapped["kind"] == "assistant" and not _has_text(mapped) and _has_text(old):
                    mapped["blocks"] = old["blocks"]        # keep streamed text if the final record has none
                self.items.put(mapped)
                await self.flush()
        elif method == "item/agentMessage/delta":
            self._append(p.get("itemId"), "text", p.get("delta") or "")
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self._append(p.get("itemId"), "thinking", p.get("delta") or "")
        elif method == "item/commandExecution/outputDelta":
            found = self.items.get(f"c:{p.get('itemId')}")
            if found:
                for block in found["blocks"]:
                    if block.get("type") == "tool":
                        block["partialOutput"] = (block.get("partialOutput") or "") + (p.get("delta") or "")
                self.items.touch(found["key"])
                self.flush_soon()
        elif method == "turn/plan/updated":
            turn = p.get("turnId") or "turn"
            key = self._plans.setdefault(turn, f"c:{turn}:plan")
            self.items.put(plan_item(key, p.get("plan") or [], p.get("explanation")))
            await self.flush()
        elif method == "turn/completed":
            await self._turn_completed(p.get("turn") or {})
        elif method == "thread/tokenUsage/updated":
            usage = p.get("tokenUsage") or {}
            last = usage.get("last") or {}
            self.context = {"used": (last.get("inputTokens") or 0) + (last.get("outputTokens") or 0),
                            "window": usage.get("modelContextWindow")}
        elif method == "account/rateLimits/updated":
            self.rate_limits = p.get("rateLimits")
            await self.emit({"ev": "rate_limit", "info": self.rate_limits})
        elif method == "error":
            err = p.get("error") or {}
            if p.get("willRetry"):
                await self.emit({"ev": "notice", "level": "warning", "text": f"{err.get('message') or 'error'} — retrying"})
        elif method == "warning":
            await self.emit({"ev": "notice", "level": "warning", "text": str(p.get("message") or p.get("summary") or p)})
        elif method == "thread/status/changed":
            status = p.get("status") or {}
            flags = status.get("activeFlags") or []
            self.status = "waiting" if "waitingOnApproval" in flags else status.get("type")
        elif method == "thread/name/updated":
            self.title = p.get("name") or self.title

    def _append(self, item_id: str | None, block_type: str, delta: str) -> None:
        key = f"c:{item_id}"
        item = self.items.get(key)
        if item is None:
            item = self.items.put(assistant_item(key, [{"type": block_type, "text": "", **({"redacted": False} if block_type == "thinking" else {})}],
                                                 model=self.model))
        for block in item["blocks"]:
            if block.get("type") == block_type:
                block["text"] = (block.get("text") or "") + delta
                if block_type == "thinking":
                    block["redacted"] = False
                break
        self.items.touch(key)
        self.flush_soon()

    async def _turn_completed(self, turn: dict[str, Any]) -> None:
        status = turn.get("status")
        if turn.get("id") and turn.get("id") != self.turn_id and self.turn_id is not None:
            log.debug("completed turn %s is not the current one (%s)", turn.get("id"), self.turn_id)
        if status == "interrupted":
            self.mark_unfinished_tools("interrupted")
        elif status == "failed":
            self.mark_unfinished_tools("failed")
        self._turn_end_items(turn)
        self.cancel_permissions()
        result = self.turn_finished("success" if status == "completed" else str(status), error=status == "failed", stop_reason=status)
        self.turn_id = None
        await self.flush()
        await self.emit({"ev": "result", "result": result, "text": self.last_assistant_text()[:400]})
        await self.emit_state()

    # ---- what Codex asks ----------------------------------------------------------------

    async def _request(self, method: str, params: Any) -> Any:
        p = params or {}
        item_id = str(p.get("itemId") or "")
        request_id = f"codex-{uuidlib.uuid4().hex[:12]}"
        if method == "item/commandExecution/requestApproval":
            answer = await self.ask({"requestId": request_id, "toolName": "Bash", "toolUseId": item_id,
                                     "input": {"command": unwrap_command(p.get("command") or ""), "cwd": p.get("cwd")},
                                     "reason": p.get("reason"), "title": None})
            return {"decision": _decision(answer)}
        if method == "item/fileChange/requestApproval":
            found = self.items.get(f"c:{item_id}")
            changes = next((b["input"].get("changes") for b in (found or {}).get("blocks", ()) if b.get("type") == "tool"), None) or []
            answer = await self.ask({"requestId": request_id, "toolName": "ApplyPatch", "toolUseId": item_id,
                                     "input": {"changes": changes, "grantRoot": p.get("grantRoot")}, "reason": p.get("reason")})
            return {"decision": _decision(answer)}
        if method == "item/tool/requestUserInput":
            questions = p.get("questions") or []
            answer = await self.ask({"requestId": request_id, "toolName": "AskUserQuestion", "toolUseId": item_id,
                                     "input": {"questions": [{"id": q.get("id"), "header": q.get("header"), "question": q.get("question"),
                                                              "options": [{"label": o.get("label"), "description": o.get("description")}
                                                                          for o in q.get("options") or ()],
                                                              "multiSelect": False} for q in questions]}})
            answers = answer.get("answers") or {}
            return {"answers": {q.get("id"): {"answers": _as_list(answers.get(q.get("id")))} for q in questions}}
        if method == "mcpServer/elicitation/request":
            answer = await self.ask({"requestId": request_id, "toolName": f"mcp__{p.get('serverName') or 'server'}",
                                     "input": {k: v for k, v in p.items() if k not in ("threadId", "turnId")},
                                     "reason": p.get("message")})
            return {"action": "accept" if answer.get("allow") else ("cancel" if answer.get("cancelled") else "decline"),
                    "content": None, "_meta": None}
        if method == "item/permissions/requestApproval":
            answer = await self.ask({"requestId": request_id, "toolName": "Permissions", "toolUseId": item_id,
                                     "input": {"permissions": p.get("permissions"), "cwd": p.get("cwd")}, "reason": p.get("reason")})
            if answer.get("allow"):
                return {"permissions": p.get("permissions") or {}, "scope": "session" if answer.get("scope") == "session" else "turn"}
            raise JsonRpcError(-32000, "declined by the user")
        raise JsonRpcError(-32601, f"{method} is not supported by this client")


def _decision(answer: dict[str, Any]) -> str:
    if answer.get("cancelled"):
        return "cancel"
    if not answer.get("allow"):
        return "decline"
    return "acceptForSession" if answer.get("scope") == "session" else "accept"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _has_text(item: dict[str, Any]) -> bool:
    return any((b.get("text") or "").strip() for b in item.get("blocks") or () if b.get("type") in ("text", "thinking"))


async def probe_models(codex: str, cwd: str, env: dict[str, str]) -> list[dict[str, Any]]:
    """Codex's model list without a session: a short-lived app-server that starts no thread."""
    async def ignore(_driver: Driver, _event: dict[str, Any]) -> None:
        return None

    driver = CodexDriver(cwd=cwd, sink=ignore, env=env, codex=codex)
    driver.peer = JsonRpcPeer([codex, "app-server"], cwd=cwd, env=env, version=False)
    await driver.peer.start()
    try:
        await driver.peer.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": None}, timeout=60)
        await driver.peer.notify("initialized")
        return await driver.models()
    finally:
        await driver.peer.stop()
