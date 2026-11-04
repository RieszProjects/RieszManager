"""Central state: transcripts, live processes, hosted processes and event fan-out."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any, Awaitable, Callable

from typing import TYPE_CHECKING

from .config import Config
from .live import LiveProcess, scan_live
from .process import ClaudeProcess, ControlError
from .projects import ProjectResolver
from .session_prefs import SessionPrefs
from .usage import UsageMonitor
from .attention import LONG_TURN_SECONDS, describe_request, session_title
from .store import SessionStore

if TYPE_CHECKING:
    from .delivery import Delivery

log = logging.getLogger(__name__)

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]

TAIL_INTERVAL = 0.35
LIVE_INTERVAL = 1.5
INDEX_INTERVAL = 3.0
REAP_INTERVAL = 30.0
KEEP_ALIVE_INTERVAL = 30.0
# A kept-alive process that ends within this many seconds of being started counts as a crash.
KEEP_ALIVE_CRASH_WINDOW = 600
RESET_ITEMS = 160
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


class Hub:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = SessionStore(cfg.projects_dir)
        self.projects = ProjectResolver(cfg)
        self.hosted: dict[str, ClaudeProcess] = {}
        self.live: dict[str, LiveProcess] = {}
        self._session_subs: dict[str, set[Subscriber]] = {}
        self._global_subs: set[Subscriber] = set()
        self._tasks: list[asyncio.Task[Any]] = []
        self._tail_wakeup: dict[str, asyncio.Event] = {}
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self.delivery: Delivery | None = None
        self.prefs = SessionPrefs(cfg.state_dir / "session_settings.json")
        self.usage = UsageMonitor(cfg)
        # Terminal sessions: when each one started working (to spot long turns finishing).
        self._external_busy_since: dict[str, float] = {}
        # Sessions to keep running: (last start attempt, consecutive quick deaths).
        self._revive_state: dict[str, tuple[float, int]] = {}
        self.reload_pending = False
        self.reload_ready = asyncio.Event()

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.store.refresh()
        self.live = scan_live(self.cfg.live_sessions_dir)
        self._tasks = [
            asyncio.create_task(self._live_loop()),
            asyncio.create_task(self._index_loop()),
            asyncio.create_task(self._reap_loop()),
            asyncio.create_task(self._keep_alive_loop()),
        ]

    # ---- graceful reload ------------------------------------------------------

    def request_reload(self) -> None:
        if not self.reload_pending:
            log.info("reload requested; waiting until all hosted sessions are idle")
            self.reload_pending = True
            self._tasks.append(asyncio.create_task(self._reload_when_idle()))

    def _all_idle(self) -> bool:
        if self.delivery is not None and self.delivery.held:
            return False
        return all(not p.alive or not p.working for p in self.hosted.values())

    async def _reload_when_idle(self) -> None:
        last_note = 0.0
        while True:
            if time.time() - last_note > 300:
                working = [sid[:8] for sid, p in self.hosted.items() if p.alive and p.working]
                if working:
                    log.info("reload waiting for sessions with work in progress: %s", ", ".join(working))
                last_note = time.time()
            if self._all_idle():
                # Two consecutive idle observations, so a turn that is just starting is not cut off.
                await asyncio.sleep(1.0)
                if self._all_idle():
                    break
            await asyncio.sleep(1.0)
        log.info("all hosted sessions idle; reloading")
        self.reload_ready.set()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*(p.stop() for p in list(self.hosted.values())), return_exceptions=True)

    # ---- subscriptions ------------------------------------------------------

    def subscribe_global(self, sub: Subscriber) -> None:
        self._global_subs.add(sub)

    def unsubscribe_global(self, sub: Subscriber) -> None:
        self._global_subs.discard(sub)

    def subscribe(self, session_id: str, sub: Subscriber) -> None:
        subs = self._session_subs.setdefault(session_id, set())
        subs.add(sub)
        if session_id not in self._tail_wakeup:
            self._tail_wakeup[session_id] = asyncio.Event()
            self._tasks.append(asyncio.create_task(self._tail_loop(session_id)))

    def unsubscribe(self, session_id: str, sub: Subscriber) -> None:
        subs = self._session_subs.get(session_id)
        if subs is not None:
            subs.discard(sub)

    async def _send(self, subs: set[Subscriber] | None, event: dict[str, Any]) -> None:
        if not subs:
            return
        dead = []
        for sub in list(subs):
            try:
                await sub(event)
            except Exception:  # noqa: BLE001
                dead.append(sub)
        for sub in dead:
            subs.discard(sub)

    async def broadcast_session(self, session_id: str, event: dict[str, Any]) -> None:
        await self._send(self._session_subs.get(session_id), {**event, "sessionId": session_id})

    async def broadcast_global(self, event: dict[str, Any]) -> None:
        await self._send(self._global_subs, event)

    # ---- transcript tailing -------------------------------------------------

    async def catch_up(self, session_id: str) -> Any:
        """Read new transcript lines and broadcast changed items to subscribers."""
        result = self.store.view(session_id)
        if result is None:
            return None
        view, changed, reset = result
        if reset:
            items = view.items[-RESET_ITEMS:]
            await self.broadcast_session(session_id, {"ev": "reset", "items": items,
                                                      "hasMore": len(view.items) > len(items)})
        elif changed:
            await self.broadcast_session(session_id, {"ev": "items", "items": changed})
        return view

    async def _tail_loop(self, session_id: str) -> None:
        wakeup = self._tail_wakeup[session_id]
        try:
            while self._session_subs.get(session_id):
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=TAIL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                wakeup.clear()
                try:
                    await self.catch_up(session_id)
                except Exception:  # noqa: BLE001
                    log.exception("tail failed for %s", session_id)
        finally:
            self._tail_wakeup.pop(session_id, None)
            self._session_subs.pop(session_id, None)

    def _poke(self, session_id: str) -> None:
        event = self._tail_wakeup.get(session_id)
        if event is not None:
            event.set()

    # ---- background loops ---------------------------------------------------

    async def _live_loop(self) -> None:
        while True:
            await asyncio.sleep(LIVE_INTERVAL)
            try:
                current = scan_live(self.cfg.live_sessions_dir)
            except Exception:  # noqa: BLE001
                continue
            before = {sid: (p.pid, p.status) for sid, p in self.live.items()}
            after = {sid: (p.pid, p.status) for sid, p in current.items()}
            self.live = current
            await self._external_attention(before, current)
            if self.delivery is not None and self.delivery.held:
                await self.delivery.on_live_changed()
            if before != after:
                await self.broadcast_global({"ev": "live", "live": self.live_list()})
                for sid in set(before) | set(after):
                    if before.get(sid) != after.get(sid):
                        await self.broadcast_session(sid, {"ev": "state", "state": self.session_state(sid)})

    async def _index_loop(self) -> None:
        while True:
            await asyncio.sleep(INDEX_INTERVAL)
            if not self._global_subs:
                continue
            try:
                changed = self.store.refresh()
            except Exception:  # noqa: BLE001
                continue
            if changed:
                sessions = [self.session_summary(sid) for sid in changed if sid in self.store.entries]
                removed = [sid for sid in changed if sid not in self.store.entries]
                await self.broadcast_global({"ev": "sessions", "sessions": sessions, "removed": removed})

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            now = time.time()
            for sid, proc in list(self.hosted.items()):
                idle = now - proc.last_activity
                # last_activity moves with every message from claude, including the
                # task events, so the idle time counts from when the last task ended.
                if proc.alive and not proc.working and idle > self.cfg.idle_timeout and not self.prefs.keep_alive(sid):
                    log.info("stopping idle claude process for %s", sid)
                    await proc.stop()

    # ---- keep running ---------------------------------------------------------

    async def _keep_alive_loop(self) -> None:
        """Start the sessions marked "keep running" whenever their process is not running:
        after a runner restart or reload, and after a crash (backing off if it keeps dying)."""
        await asyncio.sleep(3.0)
        while True:
            ids = self.prefs.keep_alive_ids()
            if ids:
                self.store.refresh()
            for sid in ids:
                try:
                    await self._revive(sid)
                except Exception:  # noqa: BLE001
                    log.exception("keep-alive: failed to start %s", sid)
            await asyncio.sleep(KEEP_ALIVE_INTERVAL)

    async def _revive(self, session_id: str) -> None:
        if self.reload_pending or self.reload_ready.is_set():
            return
        proc = self.hosted.get(session_id)
        if (proc is not None and proc.alive) or session_id in self.live or session_id not in self.store.entries:
            return  # running here, running in a terminal, or gone
        now = time.time()
        last, deaths = self._revive_state.get(session_id, (0.0, 0))
        if last and now - last < min(60 * 2 ** deaths, 1800):
            return
        deaths = deaths + 1 if last and now - last < KEEP_ALIVE_CRASH_WINDOW else 0
        self._revive_state[session_id] = (now, deaths)
        try:
            await self.ensure_process(session_id)
            log.info("keep-alive: started %s", session_id)
        except ControlError as exc:
            log.info("keep-alive: cannot start %s: %s", session_id, exc)

    async def set_keep_alive(self, session_id: str, on: bool) -> str | None:
        """Mark a session to keep running (starting it now) or not. Returns a note when it
        could not be started right away."""
        if session_id not in self.hosted and session_id not in self.store.entries:
            self.store.refresh()
            if session_id not in self.store.entries:
                raise ControlError("unknown session")
        self.prefs.set_keep_alive(session_id, on)
        self._revive_state.pop(session_id, None)
        note = None
        if on and session_id not in self.live:
            try:
                await self.ensure_process(session_id)
            except ControlError as exc:
                note = str(exc)
        elif on:
            note = "the session is running in a terminal; it will be kept running here once that process ends"
        await self.broadcast_global({"ev": "sessions", "sessions": [self.session_summary(session_id)], "removed": []})
        await self.broadcast_session(session_id, {"ev": "state", "state": self.session_state(session_id)})
        return note

    # ---- views --------------------------------------------------------------

    def live_list(self) -> list[dict[str, Any]]:
        hosted_pids = {p.pid for p in self.hosted.values() if p.alive}
        out = []
        for proc in self.live.values():
            data = proc.to_dict()
            data["hosted"] = proc.pid in hosted_pids
            out.append(data)
        return out

    def session_summary(self, session_id: str) -> dict[str, Any]:
        entry = self.store.entries.get(session_id)
        data = entry.summary() if entry else {"sessionId": session_id}
        data["project"] = self.projects.project_id_for(data.get("cwd"))
        data["live"] = self._live_info(session_id)
        data["keepAlive"] = self.prefs.keep_alive(session_id)
        return data

    def _live_info(self, session_id: str) -> dict[str, Any] | None:
        hosted = self.hosted.get(session_id)
        if hosted is not None and hosted.alive:
            return {"hosted": True, "busy": hosted.turn_active, "pid": hosted.pid,
                    "waiting": bool(hosted.permissions)}
        live = self.live.get(session_id)
        if live is not None:
            held = len(self.delivery.held.get(session_id, [])) if self.delivery else 0
            return {"hosted": False, "busy": live.status == "busy", "pid": live.pid,
                    "kind": live.kind, "entrypoint": live.entrypoint, "status": live.status, "held": held}
        return None

    def resume_settings(self, session_id: str) -> tuple[str | None, str | None]:
        """(effort, permission mode) a resumed session should start with: the ones it used last.

        Takes the newer of what this runner remembered for the session and what
        its transcript recorded (the transcript wins when the session was
        continued elsewhere afterwards, e.g. in a terminal).
        """
        entry = self.store.entries.get(session_id)
        meta = entry.scanner.meta if entry else None
        stored = self.prefs.get(session_id) or {}
        transcript_newer = entry is not None and (not stored or entry.mtime > stored.get("time", 0) + 1.0)
        if transcript_newer and meta is not None:
            effort = meta.effort or stored.get("effort")
            mode = meta.permission_mode or stored.get("permissionMode")
        else:
            effort = stored.get("effort") or (meta.effort if meta else None)
            mode = stored.get("permissionMode") or (meta.permission_mode if meta else None)
        return (effort if effort in EFFORT_LEVELS else None, mode or None)

    def resume_effort(self, session_id: str) -> str | None:
        return self.resume_settings(session_id)[0]

    def _with_meta(self, session_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """Fill model/effort/mode from the transcript where the process has not reported them."""
        entry = self.store.entries.get(session_id)
        if entry is not None:
            meta = entry.scanner.meta
            effort, mode = self.resume_settings(session_id)
            for key, value in (("model", meta.model), ("effort", effort), ("permissionMode", mode)):
                if not state.get(key):
                    state[key] = value
        state["keepAlive"] = self.prefs.keep_alive(session_id)
        return state

    def session_state(self, session_id: str) -> dict[str, Any]:
        hosted = self.hosted.get(session_id)
        if hosted is not None and hosted.alive:
            return self._with_meta(session_id, hosted.snapshot())
        live = self.live.get(session_id)
        queue = self.delivery.queue_entries(session_id) if self.delivery else []
        state: dict[str, Any] = {"hosted": False, "alive": False, "busy": False, "queue": queue,
                                 "permissions": [], "partial": None}
        if live is not None:
            state.update(alive=True, external=True, pid=live.pid, busy=live.status == "busy",
                         status=live.status, kind=live.kind, entrypoint=live.entrypoint)
        if hosted is not None:
            state["exitCode"] = hosted.exit_code
            state["lastResult"] = hosted.last_result
        return self._with_meta(session_id, state)

    # ---- hosted processes ---------------------------------------------------

    async def _sink(self, proc: ClaudeProcess, event: dict[str, Any]) -> None:
        ev = event.get("ev")
        if ev == "session_renamed":
            old, new = event["from"], event["to"]
            if self.hosted.get(old) is proc:
                del self.hosted[old]
            self.hosted[new] = proc
            await self.broadcast_session(old, {"ev": "forked", "to": new})
            return
        if ev == "transcript":
            self._poke(proc.session_id)
            return
        if ev == "rate_limit":
            self.usage.note_rate_limit(event.get("info"))
            await self.broadcast_global({"ev": "usage_changed"})
        if ev in ("permission", "result", "exit"):
            await self._attention_from(proc, event)
        if ev == "exit":
            self._remember(proc)
            self._poke(proc.session_id)
            await self.broadcast_global({"ev": "live", "live": self.live_list()})
        if ev == "state":
            self._remember(proc)
            event = {**event, "state": self._with_meta(proc.session_id, dict(event["state"]))}
        await self.broadcast_session(proc.session_id, event)
        if ev == "state":
            summary_live = self._live_info(proc.session_id)
            await self.broadcast_global({"ev": "session_live", "sessionId": proc.session_id, "live": summary_live})

    # ---- attention (push notifications) ---------------------------------------

    async def _attention(self, session_id: str, kind: str, **fields: Any) -> None:
        title = session_title(self.session_summary(session_id))
        await self.broadcast_global({"ev": "attention", "kind": kind, "sessionId": session_id, "title": title, **fields})

    async def _attention_from(self, proc: ClaudeProcess, event: dict[str, Any]) -> None:
        ev = event.get("ev")
        if ev == "permission":
            kind, detail = describe_request(event.get("request") or {})
            await self._attention(proc.session_id, kind, detail=detail)
        elif ev == "result":
            result = event.get("result") or {}
            seconds = (result.get("durationMs") or 0) / 1000
            if seconds >= LONG_TURN_SECONDS:
                await self._attention(proc.session_id, "done", seconds=round(seconds), isError=bool(result.get("isError")),
                                      detail=" ".join(str(event.get("text") or "").split())[:160])
        elif ev == "exit":
            unexpected = not event.get("requested") and (event.get("code") not in (0, None) or event.get("duringTurn"))
            if unexpected:
                await self._attention(proc.session_id, "exited", code=event.get("code"))

    async def _external_attention(self, before: dict[str, Any], current: dict[str, LiveProcess]) -> None:
        """Terminal sessions: a long stretch of work ending is worth a notification too."""
        now = time.time()
        for sid, live in current.items():
            if sid in self.hosted:
                continue
            busy = live.status == "busy"
            since = self._external_busy_since.get(sid)
            if busy and since is None:
                self._external_busy_since[sid] = now
            elif not busy and since is not None:
                del self._external_busy_since[sid]
                if now - since >= LONG_TURN_SECONDS and sid in before:
                    await self._attention(sid, "done", seconds=round(now - since), isError=False, detail="", external=True)
        for sid in list(self._external_busy_since):
            if sid not in current:
                del self._external_busy_since[sid]

    def _remember(self, proc: ClaudeProcess) -> None:
        effort = proc.effort if proc.effort in EFFORT_LEVELS else None
        self.prefs.remember(proc.session_id, effort=effort, permissionMode=proc.permission_mode)

    def get_hosted(self, session_id: str) -> ClaudeProcess | None:
        proc = self.hosted.get(session_id)
        if proc is not None and proc.alive:
            return proc
        return None

    async def ensure_process(
        self,
        session_id: str,
        *,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
    ) -> ClaudeProcess:
        """Return a running hosted process for an existing session, resuming it if needed."""
        lock = self._spawn_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            proc = self.get_hosted(session_id)
            if proc is not None:
                return proc
            if self.reload_ready.is_set():
                raise ControlError("the runner is restarting; try again in a few seconds")
            live = self.live.get(session_id)
            if live is not None:
                raise ControlError("this session is running in another Claude Code process")
            self.store.refresh()  # pick up the transcript's latest model/effort/mode
            entry = self.store.get(session_id)
            if entry is None:
                raise ControlError("unknown session")
            meta = entry.scanner.meta
            cwd = meta.cwd
            if not cwd or not Path(cwd).is_dir():
                raise ControlError("the session's working directory no longer exists")
            last_effort, last_mode = self.resume_settings(session_id)
            if last_mode == "bypassPermissions" and not self.cfg.allow_bypass:
                last_mode = None
            proc = ClaudeProcess(
                self.cfg,
                session_id=session_id,
                cwd=cwd,
                resume_from=session_id,
                model=model,
                permission_mode=permission_mode or last_mode or self.cfg.default_permission_mode,
                effort=effort or last_effort,
                sink=self._sink,
            )
            self.hosted[session_id] = proc
            try:
                await proc.start()
            except Exception:
                self.hosted.pop(session_id, None)
                raise
            await self.broadcast_global({"ev": "live", "live": self.live_list()})
            return proc

    async def create_session(
        self,
        cwd: str,
        *,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
    ) -> ClaudeProcess:
        if self.reload_ready.is_set():
            raise ControlError("the runner is restarting; try again in a few seconds")
        if not Path(cwd).is_dir():
            raise ControlError("directory does not exist")
        session_id = str(uuidlib.uuid4())
        proc = ClaudeProcess(
            self.cfg,
            session_id=session_id,
            cwd=cwd,
            model=model,
            permission_mode=permission_mode,
            effort=effort,
            sink=self._sink,
        )
        self.hosted[session_id] = proc
        try:
            await proc.start()
        except Exception:
            self.hosted.pop(session_id, None)
            raise
        return proc

    async def fork_session(self, session_id: str, *, model: str | None = None,
                           permission_mode: str | None = None) -> ClaudeProcess:
        """Start a new session that continues from a copy of an existing one."""
        if self.reload_ready.is_set():
            raise ControlError("the runner is restarting; try again in a few seconds")
        entry = self.store.get(session_id)
        if entry is None or not entry.scanner.meta.cwd:
            raise ControlError("unknown session")
        new_id = str(uuidlib.uuid4())
        proc = ClaudeProcess(
            self.cfg,
            session_id=new_id,
            cwd=entry.scanner.meta.cwd,
            resume_from=session_id,
            fork=True,
            model=model,
            permission_mode=permission_mode or self.resume_settings(session_id)[1],
            effort=self.resume_effort(session_id),
            sink=self._sink,
        )
        self.hosted[new_id] = proc
        try:
            await proc.start()
        except Exception:
            self.hosted.pop(new_id, None)
            raise
        return proc

    async def rename(self, session_id: str, title: str) -> None:
        """Set a session's custom title, through its claude process when we host it."""
        proc = self.get_hosted(session_id)
        done = False
        if proc is not None:
            try:
                await proc.control("rename_session", title=title, source="host", session_id=proc.session_id)
                done = True
            except ControlError as exc:
                log.info("rename via claude failed for %s (%s); writing the record directly", session_id, exc)
        if not done:
            record = {
                "type": "custom-title",
                "customTitle": title,
                "sessionId": session_id,
                "uuid": str(uuidlib.uuid4()),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            }
            if not await asyncio.to_thread(self.store.append_record, session_id, record):
                raise ControlError("unknown session")
        # Publish promptly instead of waiting for the next index scan.
        for _ in range(10):
            self.store.refresh()
            entry = self.store.entries.get(session_id)
            if entry is not None and entry.scanner.meta.custom_title == title:
                break
            await asyncio.sleep(0.1)
        if session_id in self.store.entries:
            await self.broadcast_global({"ev": "sessions", "sessions": [self.session_summary(session_id)], "removed": []})

    async def stop_process(self, session_id: str) -> bool:
        proc = self.hosted.get(session_id)
        if proc is None or not proc.alive:
            return False
        await proc.stop()
        return True
