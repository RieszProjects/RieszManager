"""Delivering prompts to a session, wherever it is currently running.

* Hosted here: the prompt goes straight to our claude process (queued by claude
  itself while a turn is running).
* Not running anywhere: the session is resumed here first.
* Running in another Claude Code process (a terminal, a background agent):
  claude offers no way to inject user input into that process, so when it is
  idle it is asked to exit (SIGTERM) and the session is resumed here; while it
  is busy, prompts are held and delivered as soon as it becomes idle or exits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .history import PromptHistory
from .procs import running_commands
from .process import ClaudeProcess, ControlError
from .uploads import compose_content

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

EXIT_WAIT_SECONDS = 10.0


@dataclass
class HeldPrompt:
    uuid: str
    text: str
    attachments: list[dict[str, Any]]
    preview: str
    options: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def entry(self) -> dict[str, Any]:
        return {"uuid": self.uuid, "preview": self.preview[:300], "state": "held", "ts": self.ts}


class Delivery:
    def __init__(self, hub: Hub, history: PromptHistory) -> None:
        self.hub = hub
        self.history = history
        self.held: dict[str, list[HeldPrompt]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    # ---- public API -----------------------------------------------------------

    async def send(
        self,
        session_id: str,
        text: str,
        attachments: list[dict[str, Any]],
        message_uuid: str,
        *,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Deliver a prompt. Returns {"uuid", "held": bool}."""
        options = {"model": model, "permission_mode": permission_mode, "effort": effort}
        proc = self.hub.get_hosted(session_id)
        if proc is None:
            live = self.hub.live.get(session_id)
            if live is not None and self._external_working(live):
                preview = text.strip() or f"[{len(attachments)} 个附件]"
                self.held.setdefault(session_id, []).append(
                    HeldPrompt(message_uuid, text, attachments, preview, options)
                )
                await self._announce(session_id)
                return {"uuid": message_uuid, "held": True}
            async with self._lock(session_id):
                proc = await self.takeover(session_id, **options)
        await self._write(proc, text, attachments, message_uuid)
        return {"uuid": message_uuid, "held": False}

    async def send_new(self, proc: ClaudeProcess, text: str, attachments: list[dict[str, Any]], message_uuid: str) -> None:
        await self._write(proc, text, attachments, message_uuid)

    async def takeover(self, session_id: str, **options: Any) -> ClaudeProcess:
        """Make sure the session runs here, stopping an idle external process if needed."""
        proc = self.hub.get_hosted(session_id)
        if proc is not None:
            return proc
        live = self.hub.live.get(session_id)
        if live is not None:
            if live.status == "busy":
                raise ControlError("the session is busy in another process; wait until it is idle")
            if running_commands(live.pid):
                raise ControlError("the session still has commands running in the background in another "
                                   "process; wait until they finish")
            await self._stop_external(live.pid)
            self.hub.live.pop(session_id, None)
        return await self.hub.ensure_process(session_id, **options)

    def queue_entries(self, session_id: str) -> list[dict[str, Any]]:
        return [h.entry() for h in self.held.get(session_id, [])]

    async def cancel(self, session_id: str, message_uuid: str) -> bool:
        held = self.held.get(session_id) or []
        kept = [h for h in held if h.uuid != message_uuid]
        if len(kept) == len(held):
            return False
        if kept:
            self.held[session_id] = kept
        else:
            self.held.pop(session_id, None)
        await self._announce(session_id)
        return True

    async def on_live_changed(self) -> None:
        """Flush held prompts of sessions whose external process went idle or exited."""
        for session_id in list(self.held):
            live = self.hub.live.get(session_id)
            if live is None or not self._external_working(live):
                asyncio.create_task(self._flush(session_id))

    # ---- internals --------------------------------------------------------------

    @staticmethod
    def _external_working(live: Any) -> bool:
        """A terminal session is busy, or idle between turns with commands still running
        in the background (taking it over would end its claude process and kill them)."""
        return live.status == "busy" or running_commands(live.pid) > 0

    async def _flush(self, session_id: str) -> None:
        lock = self._lock(session_id)
        if lock.locked():
            return
        async with lock:
            held = self.held.get(session_id)
            if not held:
                return
            try:
                proc = await self.takeover(session_id, **held[0].options)
            except ControlError as exc:
                log.info("cannot deliver held prompts to %s yet: %s", session_id, exc)
                return
            self.held.pop(session_id, None)
            for h in held:
                try:
                    await self._write(proc, h.text, h.attachments, h.uuid)
                except Exception:  # noqa: BLE001
                    log.exception("failed to deliver held prompt to %s", session_id)

    async def _write(self, proc: ClaudeProcess, text: str, attachments: list[dict[str, Any]], message_uuid: str) -> None:
        content, preview = await asyncio.to_thread(compose_content, text, attachments)
        await proc.send_user(content, message_uuid, preview)
        self.history.record(text, proc.cwd, proc.session_id)

    async def _announce(self, session_id: str) -> None:
        await self.hub.broadcast_session(session_id, {"ev": "state", "state": self.hub.session_state(session_id)})
        await self.hub.broadcast_global({"ev": "session_live", "sessionId": session_id,
                                         "live": self.hub._live_info(session_id)})

    @staticmethod
    async def _stop_external(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise ControlError("not allowed to stop the other Claude Code process") from exc
        deadline = time.monotonic() + EXIT_WAIT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        raise ControlError("the other Claude Code process did not exit")
