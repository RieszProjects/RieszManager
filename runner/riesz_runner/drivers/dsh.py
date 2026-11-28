"""DeepSeek Harness, driven through `dsh --profile sdk` (its official stdio JSON-RPC SDK interface).

dsh reports every session-log event (`session.event`) plus `session.status`
running/idle transitions; the same events are what it writes to the session
log on disk, so live updates and history go through one mapping (`DshEvents`).

What the interface does not have, and how this driver copes:
- no cancel: interrupting stops the process (dsh then records the turn as
  aborted), starts it again and resumes the session;
- model and effort are fixed when the process starts: a change restarts it
  (when the session is idle);
- no session list: sessions are found in its session-log directory;
- replies come a step at a time, not as streamed text.

The API key is not stored anywhere by this driver: the caller passes it in
`env` (DEEPSEEK_API_KEY) for each start.
dsh is a developer preview whose protocol carries no compatibility promise.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import tempfile
import time
import urllib.request
import uuid as uuidlib
from pathlib import Path
from typing import Any

from .base import (
    ToolServer,
    Capabilities,
    command,
    Driver,
    Mode,
    assistant_item,
    command_item,
    iso,
    interrupted_item,
    notice_item,
    output_item,
    tool_block,
    tool_result,
    user_item,
)
from .jsonrpc import JsonRpcPeer, PeerClosed
from ..auto_resume import dsh_failure

log = logging.getLogger(__name__)

PROVIDER = "deepseek-official"
DEFAULT_MODEL = "deepseek-flash"
EFFORTS = ["off", "low", "high", "max"]
FALLBACK_MODELS = [
    {"value": "deepseek-flash", "displayName": "DeepSeek V4.1 Flash", "supportedEffortLevels": EFFORTS},
    {"value": "deepseek-v4-pro", "displayName": "DeepSeek V4 Pro", "supportedEffortLevels": EFFORTS},
]
MODES = [
    Mode("read-only", "只读", "readonly", "只能读文件；要写文件时先问你"),
    Mode("workspace-write", "工作区可写", "auto", "可以改工作区里的文件、运行命令；超出工作区时先问你"),
    Mode("danger-full-access", "完全访问", "full", "不受沙箱限制"),
]
DEFAULT_MODE = "workspace-write"
QUIET_COMMANDS = {"permission"}

TOOL_NAMES = {
    "bash": "Bash", "edit": "Edit", "read": "Read", "read_image": "Read", "write": "Write", "glob": "Glob", "grep": "Grep",
    "web_fetch": "WebFetch", "web_search": "WebSearch", "todo_write": "TodoWrite", "ask_user_question": "AskUserQuestion",
    "job_output": "BashOutput", "job_kill": "KillShell", "apply_patch": "ApplyPatch",
}


def dsh_home(env: dict[str, str] | None = None) -> Path:
    home = (env or os.environ).get("DSH_HOME")
    return Path(home).expanduser() if home else Path.home() / ".dsh"


def _parse_args(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except ValueError:
        return {"arguments": arguments}
    return value if isinstance(value, dict) else {"arguments": value}


def _texts(content: Any) -> str:
    return "\n".join(c.get("text", "") for c in content or () if isinstance(c, dict) and c.get("type") == "text")


class DshEvents:
    """Turns dsh session-log events into conversation items. Used for live events and for history alike."""

    def __init__(self, driver: Driver) -> None:
        self.d = driver
        self.jobs: dict[str, dict[str, Any]] = {}          # running jobs by id
        self.results: set[str] = set()                     # tool calls that already have a result
        self.last_turn_end: dict[str, Any] | None = None
        self.quiet: set[str] = set()                       # commands not shown

    def apply(self, ev: dict[str, Any], *, live: bool) -> None:
        kind = ev.get("type")
        data = ev.get("data") or {}
        ts = iso(ev["time"] / 1000) if isinstance(ev.get("time"), (int, float)) else None
        items = self.d.items
        if kind == "user/message":
            if (data.get("source") or {}).get("kind") != "user":
                return                                       # runtime/time context and other injected text
            mid = str(data.get("id") or ev.get("seq"))
            uuid = getattr(self.d, "_uuids", {}).pop(mid, None)
            images = getattr(self.d, "_sent_images", {}).pop(mid, None)
            if images is None and live:
                pending = getattr(self.d, "_pending_images", None)
                if pending:
                    images = pending.pop(0)
            items.put(user_item(f"d:u:{mid}", _texts(data.get("content")), ts=ts, uuid=uuid or mid, images=images, origin="user"))
        elif kind == "assistant/message":
            self._assistant(data, ts)
        elif kind == "tool/result":
            self._tool_result(data)
        elif kind == "turn/start" and live:
            if not self.d.busy:
                self.d.turn_started()
        elif kind == "turn/end":
            self.last_turn_end = data
            reason = (data.get("reason") or {}).get("kind")
            turn = data.get("turn")
            if reason in ("aborted", "cancelled"):
                self.d.mark_unfinished_tools("interrupted")
                items.put(interrupted_item(f"d:t:{turn}:end"))
            elif reason not in (None, "completed"):
                self.d.mark_unfinished_tools(str(reason))
                detail = data.get("reason") or {}
                text = detail.get("message") or detail.get("error") or json.dumps(detail, ensure_ascii=False)
                items.put(notice_item(f"d:t:{turn}:end", f"dsh 这一轮没有完成：{text}", level="error"))
        elif kind == "job/registered":
            self.jobs[data.get("jobId")] = data
            call = (data.get("toolCall") or {}).get("callId")
            if call in self.results and live:
                self._background(data)
        elif kind == "job/settled":
            job = self.jobs.pop(data.get("jobId"), None) or data
            self.d.tasks.pop(job.get("jobId"), None)
        elif kind == "permission/preset":
            self.d.mode = data.get("preset") or self.d.mode
        elif kind == "command/run":
            if data.get("name") in QUIET_COMMANDS:
                self.quiet.add(str(data.get("commandId")))       # how the web app sets the mode: shown by its mode picker
                return
            items.put(command_item(f"d:cmd:{data.get('commandId')}", "/" + str(data.get("name") or ""), str(data.get("args") or "").strip()))
        elif kind == "command/done":
            if str(data.get("commandId")) in self.quiet:
                return
            items.put(output_item(f"d:cmd:{data.get('commandId')}:done", str(data.get("text") or ""), error=data.get("kind") != "success"))
        elif kind == "session/title":
            self.d.title = data.get("title") or self.d.title

    def _assistant(self, data: dict[str, Any], ts: str | None) -> None:
        message = data.get("message") or {}
        blocks: list[dict[str, Any]] = []
        for c in message.get("content") or ():
            ctype = c.get("type")
            if ctype == "text" and c.get("text"):
                blocks.append({"type": "text", "text": c["text"]})
            elif ctype == "reasoning" and c.get("text"):
                blocks.append({"type": "thinking", "text": c["text"], "redacted": False})
            elif ctype == "tool-call":
                name = str(c.get("name") or "")
                blocks.append(tool_block(str(c.get("id")), TOOL_NAMES.get(name, name), _parse_args(c.get("arguments"))))
        usage = data.get("usage") or None
        mapped_usage = None
        if usage:
            mapped_usage = {"input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"),
                            "cache_read_input_tokens": usage.get("cacheReadTokens"),
                            "cache_creation_input_tokens": usage.get("cacheWriteTokens")}
            self.d.context = {"used": (usage.get("inputTokens") or 0) + (usage.get("cacheReadTokens") or 0)
                              + (usage.get("cacheWriteTokens") or 0) + (usage.get("outputTokens") or 0),
                              "window": (self.d.context or {}).get("window")}
        if not blocks and not data.get("interrupted"):
            return
        key = f"d:a:{data.get('turn')}:{data.get('step')}"
        old = self.d.items.get(key)
        if old:                                              # a result may already be attached
            results = {b["id"]: b.get("result") for b in old.get("blocks") or () if b.get("type") == "tool"}
            for b in blocks:
                if b.get("type") == "tool" and results.get(b["id"]) is not None:
                    b["result"] = results[b["id"]]
        self.d.items.put(assistant_item(key, blocks, ts=ts, model=self.d.model, usage=mapped_usage,
                                        stop_reason="interrupted" if data.get("interrupted") else None))

    def _tool_result(self, data: dict[str, Any]) -> None:
        message = data.get("message") or {}
        call = str(message.get("toolCallId") or (message.get("source") or {}).get("callId") or "")
        self.results.add(call)
        found = self.d.items.tool_block(call)
        error = data.get("error")
        text = _texts(message.get("content"))
        if error and not text:
            text = error.get("reason") or error.get("code") or "error"
        result = tool_result(text, error=bool(message.get("isError")) or bool(error),
                             structured={"error": error} if error else None)
        if found:
            item, block = found
            block["result"] = result
            self.d.items.touch(item["key"])
        for job in self.jobs.values():                       # the call returned while its job runs on: background
            if (job.get("toolCall") or {}).get("callId") == call:
                self._background(job)

    def _background(self, job: dict[str, Any]) -> None:
        self.d.tasks[job.get("jobId")] = {
            "id": job.get("jobId"), "description": job.get("label") or "", "type": job.get("kind") or "job",
            "background": True, "toolUseId": (job.get("toolCall") or {}).get("callId"),
            "startedAt": (job.get("startedAt") or time.time() * 1000) / 1000,
        }


class DshDriver(Driver):
    harness = "dsh"
    capabilities = Capabilities(queue=True, interrupt_restarts=True, user_questions=True, model_switch="restart",
                                effort_switch="restart", streaming_text=False, background_tasks=True,
                                api_switch=True)

    def __init__(self, *, dsh: str = "dsh", provider: str = PROVIDER, patch: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dsh = dsh
        self.provider = provider
        # An overlay (`--patch`) for providers dsh does not mount by default (routes.dsh_patch).
        self.patch = patch
        self.model = self.model or DEFAULT_MODEL
        self.mode = self.mode or DEFAULT_MODE
        self.peer: JsonRpcPeer | None = None
        self.events = DshEvents(self)
        self._uuids: dict[str, str] = {}
        self._sent_images: dict[str, list[dict[str, Any]]] = {}
        self._pending_images: list[list[dict[str, Any]]] = []
        self._seeded_user_images: list[tuple[str, list[dict[str, Any]]]] = []
        self._questions: dict[str, str] = {}                 # dsh question request id -> our request id
        self._restart_pending = False
        self._restarting = False
        self._mode_sent = False

    def seed_user_images(self, entries: list[tuple[str, list[dict[str, Any]]]]) -> None:
        """Remember previously cached `(user_text, images)` so `load_history` keeps attachments."""
        self._seeded_user_images = list(entries)

    def _restore_seeded_user_images(self) -> None:
        if not self._seeded_user_images:
            return
        cursor = 0
        for it in self.items.items:
            if it.get("kind") != "user" or it.get("images"):
                continue
            want = (it.get("text") or "").strip()
            for k in range(cursor, len(self._seeded_user_images)):
                cached_text, cached_imgs = self._seeded_user_images[k]
                if cached_text == want:
                    it["images"] = [dict(img) for img in cached_imgs]
                    cursor = k + 1
                    break

    @property
    def pid(self) -> int | None:
        return self.peer.pid if self.peer else None

    def modes(self) -> list[Mode]:
        return MODES

    # ---- lifecycle --------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(self.env if self.env is not None else os.environ)
        env.pop("RUNNER_TOKEN", None)
        return env

    def mount_tools(self, server: ToolServer) -> str:
        """A patch list adding an entry of dsh's MCP client plugin (dsh-mcp-client) for the server.
        (A patch without `insert` only changes an entry the profile already has.)"""
        entry = {"id": f"mcp-{server.name}", "name": "@deepseek-ai/dsh-mcp-client",
                 "config": {"serverName": server.name, "transport": "stdio", **server.stdio(),
                            "toolCallTimeoutMs": 900_000}}             # browser tasks wait up to 8 minutes (default 60 s)
        return "- insert: " + json.dumps([entry], ensure_ascii=False) + "\n"        # JSON is YAML

    def _tools_patch(self, server: ToolServer) -> str:
        folder = Path(server.config_dir or tempfile.gettempdir())
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"dsh-{server.name}.yml"
        text = self.mount_tools(server)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        return str(path)

    async def start(self) -> None:
        resume = bool(self.session_id) and self.log_path() is not None
        self.session_id = self.session_id or f"rz-{uuidlib.uuid4()}"
        if resume:
            await self.load_history()
            self._mode_sent = True
        await self._spawn(resume=resume)
        self.started_at = time.time()
        self.ready = True
        await self.emit_state()

    async def _spawn(self, *, resume: bool) -> None:
        argv = [self.dsh, "--profile", "sdk", *(["--patch", self.patch] if self.patch else [])]
        for server in self.all_tools:
            argv += ["--patch", self._tools_patch(server)]
        self.peer = JsonRpcPeer(argv, cwd=self.cwd, env=self._env(), version=True,
                                on_notification=self._notification, on_request=self._request, on_exit=self._exited)
        await self.peer.start()
        self.alive = True
        params: dict[str, Any] = {"cwd": self.cwd, "provider": self.provider, "model": self.model,
                                  "approvalRequests": True, "capabilities": {"userQuestions": True}}
        if self.effort:
            params["reasoningEffort"] = self.effort
        try:
            await self.peer.request("initialize", params, timeout=120)
            if resume:
                await self.peer.request("session/resume", {"sessionId": self.session_id}, timeout=120)
        except Exception:
            await self.peer.stop()
            raise

    async def stop(self) -> None:
        self.cancel_permissions()
        if self.peer is not None:
            with contextlib.suppress(Exception):
                if self.peer.alive:
                    await self.peer.request("shutdown", {}, timeout=10)
            await self.peer.stop()

    async def _exited(self, code: int | None) -> None:
        if self._restarting:
            return
        self.alive = False
        self.ready = False
        self.exit_code = code
        during_turn = self.busy
        had_work = bool(self.tasks or self.has_pending_queue())
        if self.busy:
            self.mark_unfinished_tools("exited")
            self.busy = False
        self.cancel_permissions()
        await self.flush()
        await self.emit({"ev": "exit", "code": code, "stderr": list(self.peer.stderr_tail)[-15:] if self.peer else [],
                         "duringTurn": during_turn, "hadWork": had_work})
        await self.emit_state()

    async def restart(self) -> None:
        """Stop the process and start it again on the same session (dsh records an interrupted turn as aborted)."""
        self._restarting = True
        try:
            self.cancel_permissions()
            if self.peer is not None:
                await self.peer.stop()
            self.busy = False
            self.tasks.clear()
            await self.load_history()
            await self._spawn(resume=self.log_path() is not None)
            self._restart_pending = False
        finally:
            self._restarting = False
        await self.emit_state()

    # ---- what the user does -------------------------------------------------------------

    def _content(self, text: str, images: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for im in images or ():
            data = Path(im["path"]).read_bytes()
            blocks.append({"type": "image", "data": base64.b64encode(data).decode(), "mimeType": im.get("mime") or "image/png"})
        return blocks

    async def send(self, text: str, images: list[dict[str, Any]] | None = None, uuid: str | None = None) -> None:
        assert self.peer is not None
        if not self._mode_sent and self.mode != DEFAULT_MODE:
            await self._query(f"/permission {self.mode}")
        self._mode_sent = True
        shown_images = [{"ref": f"file:{im['path']}", "mediaType": im.get("mime") or "image/png"}
                        for im in images or () if im.get("path")]
        if shown_images:
            self._pending_images.append(shown_images)
        try:
            res = await self.peer.request("session/query", {"sessionId": self.session_id, "contentBlocks": self._content(text, images)},
                                          timeout=60)
        except Exception:
            if shown_images and self._pending_images:
                self._pending_images.pop()
            raise
        if isinstance(res, dict) and res.get("messageId"):
            mid = str(res["messageId"])
            found = self.items.get(f"d:u:{mid}")
            touched = False
            if uuid:
                self._uuids[mid] = uuid
                if found and found.get("uuid") != uuid:          # the message was shown before the receipt arrived
                    found["uuid"] = uuid
                    touched = True
            if shown_images:
                if found is None:
                    self._sent_images[mid] = shown_images
                elif not found.get("images"):
                    found["images"] = shown_images
                    touched = True
                if self._pending_images and self._pending_images[0] is shown_images:
                    self._pending_images.pop(0)
            if touched and found is not None:
                self.items.touch(found["key"])
                await self.flush()

    async def _query(self, text: str) -> Any:
        assert self.peer is not None
        return await self.peer.request("session/query", {"sessionId": self.session_id, "contentBlocks": [{"type": "text", "text": text}]},
                                       timeout=60)

    async def interrupt(self) -> None:
        if not self.busy:
            return
        await self.restart()
        result = self.turn_finished("interrupted", stop_reason="interrupted")
        await self.emit({"ev": "result", "result": result, "text": self.last_assistant_text()[:400]})
        await self.emit_state()

    def native_commands(self) -> list[dict[str, Any]]:
        # What dsh's SDK recognises besides /compact (/permission is the mode picker's).
        return [command("plan", "进入计划模式；/plan off 退出，/plan <内容> 进入后提交内容", "[off|内容]"),
                command("goal", "设定目标并自动连续推进到完成；也可 clear、edit <目标>、pause、resume",
                        "<目标>|clear|edit <目标>|pause|resume")]

    async def set_mode(self, mode: str) -> None:
        if mode not in {m.id for m in MODES}:
            raise ValueError(f"unknown mode {mode}")
        self.mode = mode
        if self.peer is not None and self.peer.alive and (self._mode_sent or self.items.items):
            await self._query(f"/permission {mode}")
            self._mode_sent = True
        await self.emit_state()

    async def set_model(self, model: str | None) -> None:
        self.model = model or DEFAULT_MODEL
        await self._apply_restart()

    async def set_effort(self, effort: str | None) -> None:
        if effort is not None and effort not in EFFORTS:
            raise ValueError(f"unknown effort {effort}")
        self.effort = effort
        await self._apply_restart()

    async def _apply_restart(self) -> None:
        if self.peer is None or not self.peer.alive:
            await self.emit_state()
            return
        if self.busy:
            self._restart_pending = True                     # after this turn
            await self.emit_state()
            return
        await self.restart()

    async def models(self) -> list[dict[str, Any]]:
        key = self._env().get("DEEPSEEK_API_KEY")
        if not key:
            return FALLBACK_MODELS
        try:
            data = await asyncio.to_thread(_fetch_models, key)
        except Exception as exc:  # noqa: BLE001 - offline or refused: the built-in list
            log.info("DeepSeek model list unavailable: %s", exc)
            return FALLBACK_MODELS
        out = []
        for m in data.get("data") or ():
            levels = (m.get("effort") or {}).get("supported_levels") or []
            out.append({"value": m.get("id"), "displayName": m.get("name") or m.get("id"),
                        "supportedEffortLevels": ["off", *levels] if levels else EFFORTS,
                        "defaultEffort": (m.get("effort") or {}).get("default_level"),
                        "contextWindow": m.get("context_window")})
        return out or FALLBACK_MODELS

    # ---- history --------------------------------------------------------------------------

    def log_path(self) -> Path | None:
        root = dsh_home(self.env) / "sessions"
        found = sorted(root.glob(f"*/{self.session_id}/session.v*.jsonl*")) if self.session_id else []
        return found[-1] if found else None

    async def load_history(self) -> None:
        path = self.log_path()
        if self.items.items:
            existing = [
                ((it.get("text") or "").strip(), list(it.get("images") or []))
                for it in self.items.items
                if it.get("kind") == "user" and it.get("images")
            ]
            if existing:
                self._seeded_user_images = existing
        self.items.clear()
        self.events = DshEvents(self)
        if path is not None:
            for ev in await read_log(path):
                if ev.get("type") != "session":
                    self.events.apply(ev, live=False)
        self._restore_seeded_user_images()
        await self.reset()

    # ---- what dsh reports -----------------------------------------------------------------

    async def _notification(self, method: str, params: Any) -> None:
        p = params or {}
        if p.get("sessionId") not in (None, self.session_id):
            return                                          # sub-agent sessions: not shown here yet
        if method == "session.event":
            self.events.apply(p.get("event") or {}, live=True)
            await self.flush()
        elif method == "session.status":
            if p.get("status") == "running":
                if not self.busy:
                    self.turn_started()
                await self.emit_state()
            elif p.get("status") == "idle" and self.busy:
                end = self.events.last_turn_end or {}
                reason = (end.get("reason") or {}).get("kind") or "completed"
                result = self.turn_finished("success" if reason == "completed" else reason, error=reason not in ("completed", "aborted"),
                                            stop_reason=reason, failure=dsh_failure(end.get("reason")))
                await self.flush()
                await self.emit({"ev": "result", "result": result, "text": self.last_assistant_text()[:400]})
                await self.emit_state()
                if self._restart_pending:
                    await self.restart()
        elif method == "user-question.cancelled":
            ours = self._questions.pop(str(p.get("requestId")), None)
            if ours:
                self.answer_permission(ours, allow=False)
        elif method in ("subagent.started", "subagent.finished"):
            await self.emit_state()

    # ---- what dsh asks ----------------------------------------------------------------------

    async def _request(self, method: str, params: Any) -> Any:
        p = params or {}
        request_id = f"dsh-{uuidlib.uuid4().hex[:12]}"
        if method == "approval/request":
            call = str(p.get("callId") or "")
            found = self.items.tool_block(call) if call else None
            name = str(p.get("toolName") or "")
            answer = await self.ask({"requestId": request_id, "toolName": TOOL_NAMES.get(name, name), "toolUseId": call or None,
                                     "input": found[1]["input"] if found else {}, "reason": p.get("reason")})
            if answer.get("cancelled"):
                return {"outcome": "cancelled"}
            return {"outcome": "allowed-once" if answer.get("allow") else "rejected"}
        if method == "user-question/ask":
            questions = p.get("questions") or []
            self._questions[str(p.get("requestId"))] = request_id
            answer = await self.ask({"requestId": request_id, "toolName": "AskUserQuestion",
                                     "input": {"questions": [{"id": q.get("id"), "header": q.get("header"), "question": q.get("question"),
                                                              "detail": q.get("detail"), "options": q.get("options") or [],
                                                              "multiSelect": bool(q.get("multiSelect"))} for q in questions]}})
            self._questions.pop(str(p.get("requestId")), None)
            if answer.get("cancelled") or not answer.get("allow"):
                return {"cancelled": True}
            answers = answer.get("answers") or {}
            out = []
            for q in questions:
                given = answers.get(q.get("id"))
                labels = {o.get("label") for o in q.get("options") or ()}
                picked = given if isinstance(given, list) else ([given] if given else [])
                out.append({"id": q.get("id"), "selected": [a for a in picked if a in labels],
                            **({"custom": " ".join(a for a in picked if a not in labels)} if any(a not in labels for a in picked) else {})})
            return {"answers": out}
        return {}


def _fetch_models(key: str) -> dict[str, Any]:
    req = urllib.request.Request("https://api.deepseek.com/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


async def read_log(path: Path) -> list[dict[str, Any]]:
    """Events of a dsh session log (zstd-compressed or plain JSONL)."""
    if path.suffix == ".zstd":
        proc = await asyncio.create_subprocess_exec("zstd", "-dc", str(path), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, limit=256 * 1024 * 1024)
        raw, _ = await proc.communicate()
    else:
        raw = await asyncio.to_thread(path.read_bytes)
    out = []
    for line in raw.decode(errors="replace").splitlines():
        with contextlib.suppress(ValueError):
            if line.strip():
                out.append(json.loads(line))
    return out


def list_sessions(cwd: str, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """dsh sessions recorded for `cwd`, newest first: {"id", "mtime", "path"} (titles need the log)."""
    out = []
    for path in (dsh_home(env) / "sessions").glob("*/*/session.v*.jsonl*"):
        try:
            header = _first_line(path)
        except (OSError, ValueError, PeerClosed):
            continue
        if header.get("cwd") == cwd:
            out.append({"id": header.get("id") or path.parent.name, "mtime": path.stat().st_mtime, "path": str(path),
                        "createdAt": (header.get("createdAt") or 0) / 1000})
    return sorted(out, key=lambda s: s["mtime"], reverse=True)


def _first_line(path: Path) -> dict[str, Any]:
    if path.suffix == ".zstd":
        import subprocess
        with subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            assert proc.stdout
            line = proc.stdout.readline()
            proc.kill()
    else:
        with path.open("rb") as f:
            line = f.readline()
    return json.loads(line)
