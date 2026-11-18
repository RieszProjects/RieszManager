"""JSON-RPC 2.0 over a child process's stdin/stdout, one message per line.

Both `codex app-server` and `dsh --profile sdk` speak this. Either side may send
requests (the harness asks the client to approve things), so the peer handles
requests in both directions as well as notifications.

Codex leaves out the `"jsonrpc": "2.0"` member; dsh includes it. `version`
chooses what this side writes; both forms are accepted when reading.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[str, Any], Awaitable[None]]
RequestHandler = Callable[[str, Any], Awaitable[Any]]

STDERR_LINES = 40
LINE_LIMIT = 256 * 1024 * 1024
EXIT_POLL_SECONDS = 0.1


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message
        self.data = data


class PeerClosed(Exception):
    """The process ended (or was stopped) while a request was waiting for its answer."""


class JsonRpcPeer:
    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        version: bool = True,
        on_notification: Handler | None = None,
        on_request: RequestHandler | None = None,
        on_exit: Callable[[int | None], Awaitable[None]] | None = None,
        trace: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.version = version
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_exit = on_exit
        self.trace = trace                      # ("in" | "out", message): recording and debugging
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_LINES)
        self.exit_code: int | None = None
        self._next_id = 0
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
            start_new_session=True,             # its own process group: stop() ends its children too
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
            asyncio.create_task(self._wait_exit()),
        ]

    # ---- sending ---------------------------------------------------------------------

    async def _write(self, message: dict[str, Any]) -> None:
        if not self.alive or self.proc is None or self.proc.stdin is None:
            raise PeerClosed("process is not running")
        if self.version:
            message = {"jsonrpc": "2.0", **message}
        if self.trace:
            self.trace("out", message)
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise PeerClosed(str(exc)) from None

    async def request(self, method: str, params: Any = None, *, timeout: float | None = 60.0) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        message: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write(message)
            return await asyncio.wait_for(fut, timeout) if timeout else await fut
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def respond(self, rid: Any, result: Any = None, error: JsonRpcError | None = None) -> None:
        if error is not None:
            payload: dict[str, Any] = {"code": error.code, "message": error.message}
            if error.data is not None:
                payload["data"] = error.data
            await self._write({"id": rid, "error": payload})
        else:
            await self._write({"id": rid, "result": {} if result is None else result})

    # ---- receiving -------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                log.warning("%s: a line was too long and is skipped", self.argv[0])
                continue
            if not line:
                break
            text = line.strip()
            if not text.startswith(b"{"):
                continue                        # stray output from the harness
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if self.trace:
                self.trace("in", message)
            await self._dispatch(message)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        has_id = "id" in message and message["id"] is not None
        if "method" in message and has_id:
            asyncio.create_task(self._serve(message))   # may wait for the user: do not block reading
        elif "method" in message:
            if self.on_notification:
                try:
                    await self.on_notification(message["method"], message.get("params"))
                except Exception:  # noqa: BLE001 - one bad event must not stop the stream
                    log.exception("handling %s failed", message.get("method"))
        elif has_id:
            fut = self._pending.get(message["id"])
            if fut is None or fut.done():
                return
            if "error" in message and message["error"] is not None:
                err = message["error"] if isinstance(message["error"], dict) else {"message": str(message["error"])}
                fut.set_exception(JsonRpcError(int(err.get("code") or -32000), str(err.get("message") or "error"), err.get("data")))
            else:
                fut.set_result(message.get("result"))

    async def _serve(self, message: dict[str, Any]) -> None:
        rid, method = message["id"], message["method"]
        try:
            if self.on_request is None:
                raise JsonRpcError(-32601, f"method not handled: {method}")
            result = await self.on_request(method, message.get("params"))
            await self.respond(rid, result)
        except JsonRpcError as exc:
            with contextlib.suppress(PeerClosed):
                await self.respond(rid, error=exc)
        except PeerClosed:
            pass
        except Exception as exc:  # noqa: BLE001 - answer rather than leave the harness waiting
            log.exception("serving %s failed", method)
            with contextlib.suppress(PeerClosed):
                await self.respond(rid, error=JsonRpcError(-32603, str(exc) or "internal error"))

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                self.stderr_tail.append(text)

    async def _wait_exit(self) -> None:
        # Not proc.wait(): it also waits for the pipes to close, and a child the harness left
        # running (a background command) can hold them open long after the harness itself ended.
        assert self.proc
        while self.proc.returncode is None:
            await asyncio.sleep(EXIT_POLL_SECONDS)
        code = self.proc.returncode
        self.exit_code = code
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PeerClosed(f"process exited with {code}"))
        self._closed.set()
        if self.on_exit:
            try:
                await self.on_exit(code)
            except Exception:  # noqa: BLE001
                log.exception("exit handler failed")

    async def wait_closed(self) -> int | None:
        await self._closed.wait()
        return self.exit_code

    def _group_alive(self) -> bool:
        if self.proc is None:
            return False
        try:
            os.killpg(self.proc.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _wait_group(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while self._group_alive() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(EXIT_POLL_SECONDS)

    async def stop(self, grace: float = 5.0) -> None:
        """Close stdin (a polite end), then SIGTERM and SIGKILL whatever is left of its process group."""
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None and proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            deadline = asyncio.get_running_loop().time() + min(grace, 2.0)
            while proc.returncode is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(EXIT_POLL_SECONDS)
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
            if not self._group_alive():
                break
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, sig)
            await self._wait_group(wait)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._closed.wait(), 5.0)
        for task in self._tasks:
            if not task.done():
                task.cancel()
