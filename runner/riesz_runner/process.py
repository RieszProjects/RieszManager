"""A `claude` child process driven over the stream-json control protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
import uuid as uuidlib
from collections import deque
from typing import Any, Awaitable, Callable

from .claude_settings import chrome_enabled_by_default
from .config import Config
from .procs import running_commands

log = logging.getLogger(__name__)

EventSink = Callable[["ClaudeProcess", dict[str, Any]], Awaitable[None]]

# Environment variables set by a parent Claude Code session. They must not leak
# into children, otherwise the child believes it is nested inside that session.
_PARENT_ENV_PREFIXES = ("CLAUDE_CODE_", "CLAUDECODE")
_PARENT_ENV_NAMES = {"CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"}

STREAM_FLUSH_SECONDS = 0.06
_EFFORT_COMMAND = re.compile(r"^/effort\s+(low|medium|high|xhigh|max)\b")
STDERR_TAIL_LINES = 60


class ControlError(RuntimeError):
    pass


class ClaudeProcess:
    """Owns one `claude -p --input-format stream-json` process for one session."""

    def __init__(
        self,
        cfg: Config,
        *,
        session_id: str,
        cwd: str,
        resume_from: str | None = None,
        fork: bool = False,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
        sink: EventSink,
    ) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.cwd = cwd
        self.resume_from = resume_from
        self.fork = fork
        self.requested_model = model
        self.requested_permission_mode = permission_mode or cfg.default_permission_mode
        self.effort = effort
        self.sink = sink

        self.proc: asyncio.subprocess.Process | None = None
        self.started_at = time.time()
        self.last_activity = time.time()
        self.alive = False
        self.exit_code: int | None = None
        # Set when the runner itself ends the process (idle reaping, takeover, reload).
        self.stop_requested = False
        self.init_info: dict[str, Any] | None = None
        self.model: str | None = model
        self.permission_mode: str | None = self.requested_permission_mode
        self.turn_active = False
        self.status: str | None = None
        self.queue: dict[str, dict[str, Any]] = {}
        self.permissions: dict[str, dict[str, Any]] = {}
        # Top-level tasks claude is running (foreground tool runs, subagents, background jobs).
        self.tasks: dict[str, dict[str, Any]] = {}
        self.partial: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)

        self._pending_controls: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._write_lock = asyncio.Lock()
        self._delta_buffer: dict[tuple[str, int], str] = {}
        self._flush_handle: asyncio.TimerHandle | None = None
        self._ready = asyncio.Event()

    # ---- lifecycle ----------------------------------------------------------

    def _command(self) -> list[str]:
        args = [
            self.cfg.claude_bin, "-p",
            "--output-format", "stream-json", "--verbose",
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--permission-prompt-tool", "stdio",
            "--permission-mode", self.requested_permission_mode,
            "--add-dir", str(self.cfg.uploads_dir),
        ]
        if self.cfg.allow_bypass:
            args.append("--allow-dangerously-skip-permissions")
        chrome = self.cfg.chrome
        if chrome == "on" or (chrome == "auto" and chrome_enabled_by_default(self.cfg.claude_home)):
            args.append("--chrome")
        elif chrome == "off":
            args.append("--no-chrome")
        if self.resume_from:
            args += ["--resume", self.resume_from]
            if self.fork:
                args += ["--fork-session", "--session-id", self.session_id]
        else:
            args += ["--session-id", self.session_id]
        if self.requested_model:
            args += ["--model", self.requested_model]
        if self.effort:
            args += ["--effort", self.effort]
        flags = self.flag_settings()
        if flags:
            args += ["--settings", json.dumps(flags)]
        args += self.cfg.extra_args
        if self.cfg.login_shell:
            shell = os.environ.get("SHELL") or "/bin/bash"
            return [shell, "-lc", 'exec "$0" "$@"', *args]
        return args

    @staticmethod
    def _child_env() -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(_PARENT_ENV_PREFIXES) and k not in _PARENT_ENV_NAMES
        }
        env.pop("RUNNER_TOKEN", None)
        return env

    async def start(self) -> None:
        self.cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
        cmd = self._command()
        log.info("spawning claude for %s in %s", self.session_id, self.cwd)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._child_env(),
            limit=256 * 1024 * 1024,
            start_new_session=True,
        )
        self.alive = True
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
            asyncio.create_task(self._wait_exit()),
        ]
        try:
            info = await self.control("initialize", timeout=90)
        except Exception:
            await self.stop()
            raise
        self.init_info = info
        self.permission_mode = info.get("current_permission_mode") or self.permission_mode
        self._ready.set()
        await self._emit_state()

    async def stop(self, grace: float = 5.0) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        self.stop_requested = True
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    @property
    def busy(self) -> bool:
        return self.turn_active or bool(self.permissions)

    def flag_settings(self) -> dict[str, Any]:
        """Settings the runner imposes on hosted sessions (on top of the user's own)."""
        flags: dict[str, Any] = {}
        if self.cfg.cross_session_inbound:
            # Headless sessions cannot ask anyone to approve a held cross-session message,
            # so "hold" would drop messages between sessions in different permission modes.
            flags["crossSessionInbound"] = self.cfg.cross_session_inbound
        return flags

    @property
    def working(self) -> bool:
        """Whether ending the process now would cut something off.

        Besides a turn in progress, a pending permission prompt and queued
        messages, this counts tasks claude is still running between turns:
        background commands, subagents and monitors. Those belong to the claude
        process and die with it, even though the session looks idle. Running
        commands are also read from the process tree, in case an event was missed.
        """
        return self.busy or bool(self.queue) or bool(self.tasks) or running_commands(self.pid) > 0

    def snapshot(self) -> dict[str, Any]:
        info = self.init_info or {}
        return {
            "hosted": True,
            "alive": self.alive,
            "ready": self._ready.is_set(),
            "pid": self.pid,
            "busy": self.turn_active,
            "status": self.status,
            "model": self.model,
            "permissionMode": self.permission_mode,
            "effort": self.effort,
            "queue": list(self.queue.values()),
            "permissions": list(self.permissions.values()),
            "tasks": list(self.tasks.values()),
            "partial": self.partial,
            "lastResult": self.last_result,
            "startedAt": self.started_at,
            "fastMode": info.get("fast_mode_state"),
            "exitCode": self.exit_code,
        }

    def commands(self) -> list[dict[str, Any]]:
        return list((self.init_info or {}).get("commands") or [])

    def models(self) -> list[dict[str, Any]]:
        return list((self.init_info or {}).get("models") or [])

    # ---- writing ------------------------------------------------------------

    async def _write(self, obj: dict[str, Any]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise ControlError("process is not running")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            proc.stdin.write(data)
            await proc.stdin.drain()
        self.last_activity = time.time()

    async def control(self, subtype: str, timeout: float = 30, **payload: Any) -> dict[str, Any]:
        request_id = f"rz-{uuidlib.uuid4().hex[:16]}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_controls[request_id] = fut
        try:
            await self._write({"type": "control_request", "request_id": request_id,
                               "request": {"subtype": subtype, **payload}})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_controls.pop(request_id, None)

    async def send_user(self, content: Any, message_uuid: str, preview: str) -> None:
        # Track `/effort <level>` typed as a prompt, so the level survives a later resume.
        if isinstance(content, str):
            match = _EFFORT_COMMAND.match(content.strip())
            if match:
                self.effort = match.group(1)
        self.queue[message_uuid] = {
            "uuid": message_uuid,
            "preview": preview[:300],
            "state": "sending",
            "ts": time.time(),
        }
        await self._emit_state()
        await self._write({
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "uuid": message_uuid,
            "origin": {"kind": "human"},
        })

    async def interrupt(self, cancel_queued: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"cancel_queued": True} if cancel_queued else {}
        return await self.control("interrupt", **payload)

    async def cancel_message(self, message_uuid: str) -> dict[str, Any]:
        result = await self.control("cancel_async_message", message_uuid=message_uuid)
        if result.get("cancelled", True):
            self.queue.pop(message_uuid, None)
            await self._emit_state()
        return result

    async def set_model(self, model: str | None) -> None:
        await self.control("set_model", model=model)
        self.model = model
        await self._emit_state()

    async def set_effort(self, level: str) -> None:
        # Sent with the runner's own flag settings so that they are kept whether claude
        # merges this update into them or replaces them.
        await self.control("apply_flag_settings", settings={**self.flag_settings(), "effortLevel": level})
        self.effort = level
        await self._emit_state()

    async def set_permission_mode(self, mode: str) -> None:
        await self.control("set_permission_mode", mode=mode)
        self.permission_mode = mode
        await self._emit_state()

    async def answer_permission(self, request_id: str, decision: dict[str, Any]) -> None:
        request = self.permissions.pop(request_id, None)
        if request is None:
            raise ControlError("permission request is no longer pending")
        await self._write({
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": decision},
        })
        await self.sink(self, {"ev": "permission_resolved", "requestId": request_id})
        await self._emit_state()

    # ---- reading ------------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        reader = self.proc.stdout
        while True:
            try:
                line = await reader.readline()
            except (asyncio.LimitOverrunError, ValueError):
                log.warning("oversized stdout line from claude (%s); skipping", self.session_id)
                continue
            if not line:
                break
            self.last_activity = time.time()
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            try:
                await self._handle(msg)
            except Exception:  # noqa: BLE001
                log.exception("error handling claude message")

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text)

    async def _wait_exit(self) -> None:
        assert self.proc
        code = await self.proc.wait()
        during_turn = self.turn_active
        self.alive = False
        self.exit_code = code
        self.turn_active = False
        self.permissions.clear()
        self.queue.clear()
        self.tasks.clear()
        self.partial = None
        for fut in self._pending_controls.values():
            if not fut.done():
                fut.set_exception(ControlError("claude process exited"))
        log.info("claude for %s exited with %s", self.session_id, code)
        await self.sink(self, {"ev": "exit", "code": code, "stderr": list(self.stderr_tail)[-15:],
                               "duringTurn": during_turn, "requested": self.stop_requested})
        await self._emit_state()

    async def _handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "stream_event":
            if msg.get("parent_tool_use_id"):
                return
            await self._handle_stream(msg.get("event") or {})
            return
        if mtype == "control_response":
            response = msg.get("response") or {}
            fut = self._pending_controls.get(response.get("request_id", ""))
            if fut and not fut.done():
                if response.get("subtype") == "success":
                    fut.set_result(response.get("response") or {})
                else:
                    fut.set_exception(ControlError(response.get("error") or "control request failed"))
            return
        if mtype == "control_request":
            await self._handle_control_request(msg)
            return
        if mtype == "control_cancel_request":
            request_id = msg.get("request_id")
            if request_id and self.permissions.pop(request_id, None) is not None:
                await self.sink(self, {"ev": "permission_resolved", "requestId": request_id})
                await self._emit_state()
            return
        if mtype == "command_lifecycle":
            await self._handle_lifecycle(msg)
            return
        if mtype == "system":
            await self._handle_system(msg)
            return
        if mtype == "result":
            self.turn_active = False
            self.status = None
            self.partial = None
            self.last_result = {
                "subtype": msg.get("subtype"),
                "isError": msg.get("is_error"),
                "durationMs": msg.get("duration_ms"),
                "costUsd": msg.get("total_cost_usd"),
                "numTurns": msg.get("num_turns"),
                "stopReason": msg.get("stop_reason"),
                "ts": time.time(),
            }
            text = msg.get("result") if isinstance(msg.get("result"), str) else ""
            await self.sink(self, {"ev": "result", "result": self.last_result, "text": text[:400]})
            await self.sink(self, {"ev": "transcript"})
            await self._emit_state()
            return
        if mtype in ("assistant", "user"):
            # The transcript file is the source of truth; nudge the hub to read it.
            if mtype == "assistant" and msg.get("parent_tool_use_id") is None:
                self.partial = None
            await self.sink(self, {"ev": "transcript"})
            return
        if mtype == "rate_limit_event":
            await self.sink(self, {"ev": "rate_limit", "info": msg.get("rate_limit_info")})

    async def _handle_system(self, msg: dict[str, Any]) -> None:
        subtype = msg.get("subtype")
        if subtype == "init":
            self.turn_active = True
            if msg.get("model"):
                self.model = msg["model"]
            if msg.get("permissionMode"):
                self.permission_mode = msg["permissionMode"]
            if isinstance(msg.get("effort"), str):
                self.effort = msg["effort"]
            new_sid = msg.get("session_id")
            if new_sid and new_sid != self.session_id:
                old = self.session_id
                self.session_id = new_sid
                await self.sink(self, {"ev": "session_renamed", "from": old, "to": new_sid})
            await self._emit_state()
        elif subtype == "status":
            self.status = msg.get("status")
            if msg.get("permissionMode"):
                self.permission_mode = msg["permissionMode"]
            await self._emit_state()
        elif subtype == "api_retry":
            await self.sink(self, {"ev": "notice", "level": "warning", "text": _retry_text(msg)})
        elif subtype in ("compact_boundary", "local_command_output", "informational"):
            await self.sink(self, {"ev": "transcript"})
        elif subtype in ("task_started", "task_updated", "task_notification", "background_tasks_changed"):
            if self._track_task(subtype, msg):
                await self._emit_state()
            if subtype == "task_notification":
                await self.sink(self, {"ev": "transcript"})
        elif subtype == "commands_changed" and self.init_info is not None:
            commands = msg.get("commands")
            if isinstance(commands, list):
                self.init_info["commands"] = commands
                await self.sink(self, {"ev": "commands"})

    def _track_task(self, subtype: str, msg: dict[str, Any]) -> bool:
        """Update `self.tasks` from a task event; returns True if anything changed."""
        task_id = msg.get("task_id")
        if subtype == "task_started" and task_id and not msg.get("parent_task_id"):
            self.tasks[task_id] = {
                "id": task_id,
                "description": msg.get("description") or "",
                "type": msg.get("subagent_type") or msg.get("task_type") or "task",
                "background": bool(msg.get("is_backgrounded")),
                "toolUseId": msg.get("tool_use_id"),
                "startedAt": time.time(),
            }
            return True
        if subtype == "task_updated" and task_id in self.tasks:
            patch = msg.get("patch") or {}
            if patch.get("status") in ("completed", "failed", "killed"):
                del self.tasks[task_id]
                return True
            if "is_backgrounded" in patch:
                self.tasks[task_id]["background"] = bool(patch["is_backgrounded"])
                return True
            return False
        if subtype == "task_notification" and task_id:
            return self.tasks.pop(task_id, None) is not None
        if subtype == "background_tasks_changed" and isinstance(msg.get("tasks"), list):
            listed = {t.get("task_id"): t for t in msg["tasks"] if isinstance(t, dict) and not t.get("parent_task_id")}
            changed = False
            for tid, task in list(self.tasks.items()):
                if task["background"] and tid not in listed:
                    del self.tasks[tid]
                    changed = True
            for tid, t in listed.items():
                if tid and tid not in self.tasks:
                    self.tasks[tid] = {"id": tid, "description": t.get("description") or "",
                                       "type": t.get("subagent_type") or t.get("task_type") or "task",
                                       "background": True, "toolUseId": None, "startedAt": time.time()}
                    changed = True
                elif tid:
                    changed |= not self.tasks[tid]["background"]
                    self.tasks[tid]["background"] = True
            return changed
        return False

    async def _handle_lifecycle(self, msg: dict[str, Any]) -> None:
        cmd_uuid = msg.get("command_uuid")
        state = msg.get("state")
        if not cmd_uuid:
            return
        if state == "queued":
            entry = self.queue.setdefault(cmd_uuid, {"uuid": cmd_uuid, "preview": "", "ts": time.time()})
            entry["state"] = "queued"
        elif state in ("started", "completed", "cancelled", "failed"):
            self.queue.pop(cmd_uuid, None)
            if state == "started":
                self.turn_active = True
        await self._emit_state()

    async def _handle_control_request(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        request = msg.get("request") or {}
        subtype = request.get("subtype")
        if subtype == "can_use_tool" and request_id:
            entry = {
                "requestId": request_id,
                "toolName": request.get("tool_name"),
                "displayName": request.get("display_name"),
                "input": request.get("input"),
                "toolUseId": request.get("tool_use_id"),
                "suggestions": request.get("permission_suggestions") or [],
                "reason": request.get("decision_reason"),
                "title": request.get("title"),
                "description": request.get("description"),
                "blockedPath": request.get("blocked_path"),
                "suppressAlwaysAllow": request.get("suppress_always_allow_rule"),
                "defaultToNo": request.get("default_to_no"),
                "ts": time.time(),
            }
            self.permissions[request_id] = entry
            await self.sink(self, {"ev": "permission", "request": entry})
            await self._emit_state()
            return
        # Anything else (hooks, SDK MCP servers, elicitation) is not supported by this host.
        log.info("unsupported control request from claude (%s): %s %s", self.session_id[:8], subtype,
                 json.dumps(request, ensure_ascii=False)[:300])
        await self._write({
            "type": "control_response",
            "response": {"subtype": "error", "request_id": request_id,
                         "error": f"{subtype} is not supported by this client"},
        })

    # ---- partial streaming ----------------------------------------------------

    async def _handle_stream(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self.partial = {"msgId": message.get("id"), "blocks": {}}
            await self.sink(self, {"ev": "stream", "op": "start", "msgId": message.get("id")})
        elif etype == "content_block_start" and self.partial is not None:
            block = event.get("content_block") or {}
            index = int(event.get("index", 0))
            btype = block.get("type")
            entry = {"type": btype, "text": "", "name": block.get("name"), "id": block.get("id")}
            self.partial["blocks"][str(index)] = entry
            await self.sink(self, {"ev": "stream", "op": "block", "msgId": self.partial["msgId"],
                                   "index": index, "blockType": btype, "name": block.get("name"),
                                   "id": block.get("id")})
        elif etype == "content_block_delta" and self.partial is not None:
            delta = event.get("delta") or {}
            index = int(event.get("index", 0))
            chunk = delta.get("text") or delta.get("thinking") or delta.get("partial_json") or ""
            if not chunk:
                return
            entry = self.partial["blocks"].get(str(index))
            if entry is not None:
                entry["text"] += chunk
            key = (self.partial["msgId"] or "", index)
            self._delta_buffer[key] = self._delta_buffer.get(key, "") + chunk
            if self._flush_handle is None:
                loop = asyncio.get_running_loop()
                self._flush_handle = loop.call_later(STREAM_FLUSH_SECONDS, lambda: asyncio.ensure_future(self._flush()))
        elif etype == "content_block_stop" and self.partial is not None:
            await self._flush()
            await self.sink(self, {"ev": "stream", "op": "block_stop", "msgId": self.partial["msgId"],
                                   "index": int(event.get("index", 0))})
        elif etype == "message_stop":
            await self._flush()

    async def _flush(self) -> None:
        self._flush_handle = None
        if not self._delta_buffer:
            return
        buffered, self._delta_buffer = self._delta_buffer, {}
        for (msg_id, index), text in buffered.items():
            await self.sink(self, {"ev": "stream", "op": "delta", "msgId": msg_id, "index": index, "text": text})

    async def _emit_state(self) -> None:
        await self.sink(self, {"ev": "state", "state": self.snapshot()})


def _retry_text(msg: dict[str, Any]) -> str:
    attempt = msg.get("attempt")
    max_retries = msg.get("max_retries")
    error = msg.get("error") or msg.get("error_status") or "API error"
    if attempt and max_retries:
        return f"{error} — retrying ({attempt}/{max_retries})"
    return str(error)
