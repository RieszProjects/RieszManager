"""Central state: transcripts, live processes, hosted processes and event fan-out."""

from __future__ import annotations

import asyncio
import os
import json
import logging
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any, Awaitable, Callable

from typing import TYPE_CHECKING

from .config import Config
from .live import LiveProcess, scan_live
from .process import ClaudeProcess, ControlError
from .tools import SubagentWatcher, server as tools_server
from .projects import ProjectResolver
from .session_prefs import SessionPrefs
from .auto_resume import AutoResume
from .deepseek_proxy import DeepSeekAnthropicProxy
from .quota_fallback import ClaudeQuotaFallback
from .balances import BalanceMonitor
from .browser import ServerBrowser, mcp_server as browser_mcp_server
from .limits import Limits
from .nodes import NodeManager
from .notes import Notes
from .shares import Shares
from .schedules import Scheduler
from .subagents import SubagentStore
from .agy_subagents import AgySubagentStore
from .usage import UsageMonitor
from .attention import LONG_TURN_SECONDS, describe_request, session_title
from .store import SessionStore
from .harnesses import HARNESSES
from .harnesses import Harnesses
from .credentials import CredentialError, Credentials
from . import routes

if TYPE_CHECKING:
    from .delivery import Delivery

log = logging.getLogger(__name__)

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]

TAIL_INTERVAL = 0.35
LIVE_INTERVAL = 1.5
INDEX_INTERVAL = 3.0
REAP_INTERVAL = 30.0
# How often the sessions with work in progress are written down, to tell after an abrupt stop
# (reboot, crash) which ones were interrupted.
WORKING_SNAPSHOT_INTERVAL = 5.0
KEEP_ALIVE_INTERVAL = 30.0
# A kept-alive process that ends within this many seconds of being started counts as a crash.
KEEP_ALIVE_CRASH_WINDOW = 600
RESET_ITEMS = 160
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _model_compatible(route: str, model: Any) -> bool:
    """Whether `model` belongs to Claude Code's `route` (prevents leaking deepseek-* to Anthropic or vice versa)."""
    if not isinstance(model, str) or not model or model == "<synthetic>":
        return False
    if route == "deepseek:key":
        return model.startswith("deepseek")
    return not model.startswith("deepseek")


class Hub:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = SessionStore(cfg.projects_dir)
        self.subagents = SubagentStore(self.store)
        self.agy_subagents = AgySubagentStore()
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
        self.balances = BalanceMonitor(cfg)
        # Terminal sessions: when each one started working (to spot long turns finishing).
        self._external_busy_since: dict[str, float] = {}
        # Sessions to keep running: (last start attempt, consecutive quick deaths).
        self._revive_state: dict[str, tuple[float, int]] = {}
        self.reload_pending = False
        self.reload_since: float | None = None
        self.reload_ready = asyncio.Event()
        self.closing = False
        # Sessions currently moving to another harness (e.g. compacting before handoff): prompts sent meanwhile
        # wait here and are delivered to the target session once the move finishes, instead of going to the old one.
        self._handing_off: set[str] = set()
        self._handoff_held: dict[str, list[dict[str, Any]]] = {}
        self._working_file = cfg.state_dir / "working.json"
        self._working_saved: set[str] | None = None
        # API keys for every harness, and which harnesses are logged in to a subscription.
        self.credentials = Credentials(cfg)
        self.balances.credentials = self.credentials
        # Sessions of other harnesses (Codex, DeepSeek Harness): see harnesses.py.
        self.tool_server = tools_server(cfg)       # Riesz Manager's own tools, for every harness (tools.py)
        self.browser = ServerBrowser(cfg)          # persistent server-side Chrome (browser.py)
        browser_mcp = browser_mcp_server(cfg)
        self.extra_tools = (browser_mcp,) if browser_mcp is not None else ()
        self.harnesses = Harnesses(self)
        self.shares = Shares(cfg.state_dir)        # files agents sent to the user (shares.py)
        self.notes = Notes(self)                   # reader sticky notes shown in the web floating window (notes.py)
        self.nodes = NodeManager(self)             # reverse-connected personal computer nodes (nodes.py)
        self.subagent_watcher = SubagentWatcher(self)  # persistent parent callbacks for subagent turns
        self.limits = Limits(self)               # other subscriptions' usage limits (limits.py)
        self.deepseek_proxy = DeepSeekAnthropicProxy()   # Anthropic-2026 -> DeepSeek compatibility proxy
        self.auto_resume = AutoResume(self)      # retry turns that failed for quota / billing / transient errors
        self.quota_fallback = ClaudeQuotaFallback(self)  # Claude subscription -> DeepSeek API fallback & restore

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.store.refresh()
        self.live = scan_live(self.cfg.live_sessions_dir)
        self._mark_interrupted()
        self.schedules = Scheduler(self)
        self._tasks = [
            asyncio.create_task(self._working_loop()),
            asyncio.create_task(self.schedules.loop()),
            asyncio.create_task(self.auto_resume.loop()),
            asyncio.create_task(self.quota_fallback.loop()),
            asyncio.create_task(self._live_loop()),
            asyncio.create_task(self._index_loop()),
            asyncio.create_task(self._reap_loop()),
            asyncio.create_task(self._keep_alive_loop()),
            asyncio.create_task(self.harnesses.loop()),
            asyncio.create_task(self.subagent_watcher.loop()),
        ]
        if self.browser.available and self.extra_tools:
            self._tasks.append(asyncio.create_task(self.browser.start_background()))

    # ---- graceful reload ------------------------------------------------------

    def request_reload(self) -> None:
        if not self.reload_pending:
            log.info("reload requested; waiting until all hosted sessions are idle")
            self.reload_pending = True
            self.reload_since = time.time()
            self._tasks.append(asyncio.create_task(self._reload_when_idle()))

    def _reload_waiting_for(self) -> list[str]:
        return [sid for sid, p in self.hosted.items() if p.alive and p.working] + self.harnesses.working_ids()

    def reload_info(self) -> dict[str, Any]:
        """A reload waiting for sessions to be idle (shown in the web app): since when, and for which."""
        if not self.reload_pending:
            return {"ev": "reload", "pending": False}
        return {"ev": "reload", "pending": True, "since": self.reload_since, "waitingFor": self._reload_waiting_for()}

    def _all_idle(self) -> bool:
        if self.delivery is not None and self.delivery.held:
            return False
        if self.subagent_watcher.has_in_flight() or self.quota_fallback.has_in_flight():
            return False
        return all(not p.alive or not p.working for p in self.hosted.values()) and not self.harnesses.working()

    async def _reload_when_idle(self) -> None:
        last_note = 0.0
        last_waiting: list[str] | None = None
        while True:
            waiting = self._reload_waiting_for()
            if waiting != last_waiting:
                last_waiting = waiting
                await self.broadcast_global(self.reload_info())
            if time.time() - last_note > 300:
                if waiting:
                    log.info("reload waiting for sessions with work in progress: %s", ", ".join(s[:8] for s in waiting))
                last_note = time.time()
            if self._all_idle():
                # Two consecutive idle observations, so a turn that is just starting is not cut off.
                await asyncio.sleep(1.0)
                if self._all_idle():
                    break
            await asyncio.sleep(1.0)
        log.info("all hosted sessions idle; reloading")
        self._save_working(set())  # nothing is interrupted by a reload
        self.reload_ready.set()

    async def close(self) -> None:
        self.closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*(p.stop() for p in list(self.hosted.values())), self.harnesses.close(),
                             self.browser.stop(), self.deepseek_proxy.close(),
                             *(self.nodes.kick_device(nid) for nid in list(self.nodes._live)),
                             return_exceptions=True)

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
            every = self.display_items(session_id, view.items)
            items = every[-RESET_ITEMS:]
            await self.broadcast_session(session_id, {"ev": "reset", "items": items, "hasMore": len(every) > len(items)})
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
                lock = self._spawn_locks.get(sid)
                if lock is not None and lock.locked():
                    continue
                idle = now - proc.last_activity
                # last_activity moves with every message from claude, including the
                # task events, so the idle time counts from when the last task ended.
                if proc.alive and not proc.working and idle > self.cfg.idle_timeout and not self.prefs.keep_alive(sid):
                    log.info("stopping idle claude process for %s", sid)
                    await proc.stop()

    # ---- interrupted sessions ---------------------------------------------------

    def _mark_interrupted(self) -> None:
        """Sessions still working when the runner last stopped were cut off: mark them."""
        try:
            saved = json.loads(self._working_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        sessions = saved.get("sessions") if isinstance(saved, dict) else None
        when = saved.get("time") if isinstance(saved, dict) and isinstance(saved.get("time"), (int, float)) else time.time()
        for sid in sessions or []:
            if isinstance(sid, str) and self.prefs.set_interrupted(sid, when):
                log.info("session %s was interrupted when the runner stopped", sid[:8])
        self._save_working(set())

    def _save_working(self, working: set[str]) -> None:
        if working == self._working_saved:
            return
        try:
            self._working_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._working_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": sorted(working), "time": time.time()}), encoding="utf-8")
            os.replace(tmp, self._working_file)
            self._working_saved = working
        except OSError as exc:
            log.warning("cannot write %s: %s", self._working_file, exc)

    async def _working_loop(self) -> None:
        while True:
            await asyncio.sleep(WORKING_SNAPSHOT_INTERVAL)
            if self.reload_ready.is_set():
                continue
            self._save_working({sid for sid, p in self.hosted.items() if p.alive and p.working} | set(self.harnesses.working_ids()))

    async def clear_interrupted(self, session_id: str) -> None:
        if self.prefs.set_interrupted(session_id, None):
            await self.broadcast_global({"ev": "sessions", "sessions": [self.session_summary(session_id)], "removed": []})

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
        if (self.prefs.get(session_id) or {}).get("handedOffTo"):
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
        hosted = self.hosted.get(session_id)
        if on and not (hosted is not None and hosted.alive):
            # self.live lists every running claude, hosted ones included; only others are "in a terminal".
            if session_id in self.live:
                note = "the session is running in a terminal; it will be kept running here once that process ends"
            else:
                try:
                    await self.ensure_process(session_id)
                except ControlError as exc:
                    note = str(exc)
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
        if self.harnesses.has(session_id):
            return self.harnesses.summary(session_id)
        entry = self.store.entries.get(session_id)
        data = entry.summary() if entry else {"sessionId": session_id}
        data["project"] = self.projects.project_id_for(data.get("cwd"))
        data["live"] = self._live_info(session_id)
        data["keepAlive"] = self.prefs.keep_alive(session_id)
        data["startedBy"] = (self.prefs.get(session_id) or {}).get("startedBy")
        data["interrupted"] = self.prefs.interrupted(session_id)
        data["subagents"], data["subagentsActive"] = self.subagents.activity(session_id)
        children = self.child_entries(session_id)
        data["subagents"] += len(children)
        data["subagentsActive"] = (data["subagentsActive"] or 0) + sum(1 for c in children if c["running"])
        data["subagentOf"] = (self.prefs.get(session_id) or {}).get("subagentOf")
        data["api"] = (self.prefs.get(session_id) or {}).get("api")
        data["handoffFrom"] = (self.prefs.get(session_id) or {}).get("handoffFrom")
        data["handedOffTo"] = (self.prefs.get(session_id) or {}).get("handedOffTo")
        data["autoResume"] = self.auto_resume.view(session_id)
        data["quotaFallback"] = self.quota_fallback.view(session_id)
        return data

    def _live_info(self, session_id: str) -> dict[str, Any] | None:
        hosted = self.hosted.get(session_id)
        if hosted is not None and hosted.alive:
            agents = [t["toolUseId"] for t in hosted.tasks.values()
                      if t.get("toolUseId") and t.get("kind") not in ("local_bash", "local_workflow")]
            return {"hosted": True, "busy": hosted.turn_active, "pid": hosted.pid,
                    "waiting": bool(hosted.permissions), "agents": agents}
        live = self.live.get(session_id)
        if live is not None:
            held = len(self.delivery.held.get(session_id, [])) if self.delivery else 0
            since = (live.started_at or 0) / 1000
            return {"hosted": False, "busy": live.status == "busy", "pid": live.pid,
                    "kind": live.kind, "entrypoint": live.entrypoint, "status": live.status, "held": held,
                    "agents": self.subagents.unfinished(session_id, since)}
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
        state["autoResume"] = self.auto_resume.view(session_id)
        state["quotaFallback"] = self.quota_fallback.view(session_id)
        held = self.handoff_queue_entries(session_id)
        if held:
            state["queue"] = [*(state.get("queue") or []), *held]
        if not state.get("api"):
            state["api"] = (self.prefs.get(session_id) or {}).get("api")
        route = state.get("api") or "anthropic:subscription"
        if not _model_compatible(route, state.get("model")):
            saved = (self.prefs.get(session_id) or {}).get("restoreModel")
            state["model"] = saved if _model_compatible(route, saved) else None
        return state

    def session_state(self, session_id: str) -> dict[str, Any]:
        if self.harnesses.has(session_id):
            return self.harnesses.state(session_id)
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
            self.quota_fallback.on_rate_limit(event.get("info"))
            await self.broadcast_global({"ev": "usage_changed"})
        if ev in ("permission", "result", "exit"):
            await self._attention_from(proc, event)
        if ev == "result":
            handled = await self.quota_fallback.on_result(proc.session_id, event.get("result") or {})
            if not handled:
                await self.auto_resume.on_result(proc.session_id, event.get("result") or {})
                self.subagent_watcher.on_result(proc.session_id, event)
        if ev == "exit":
            self._remember(proc)
            self._poke(proc.session_id)
            await self.auto_resume.on_exit(proc.session_id, during_turn=bool(event.get("duringTurn")),
                                           requested=bool(event.get("requested")))
            self.subagent_watcher.on_exit(proc.session_id, event)
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
            # Sessions from before routes existed, and terminal sessions, use the subscription.
            pref = self.prefs.get(session_id) or {}
            route = pref.get("api") or "anthropic:subscription"
            if model and not _model_compatible(route, model):
                model = None
            if model is None and _model_compatible(route, pref.get("restoreModel")):
                model = pref.get("restoreModel")
            proc = ClaudeProcess(
                self.cfg,
                session_id=session_id,
                cwd=cwd,
                resume_from=session_id,
                model=model,
                permission_mode=permission_mode or last_mode or self.cfg.default_permission_mode,
                effort=effort or last_effort,
                sink=self._sink,
                tools=self.tool_server,
                extra_tools=self.extra_tools,
                api=await self.claude_setup(route),
                browser=bool(pref.get("browser")),
            )
            self.hosted[session_id] = proc
            try:
                await proc.start()
            except Exception:
                self.hosted.pop(session_id, None)
                raise
            self.prefs.forget(session_id, "restoreModel")
            await self.broadcast_global({"ev": "live", "live": self.live_list()})
            return proc

    async def create_session(
        self,
        cwd: str,
        *,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
        api: str | None = None,
        browser: bool = False,
    ) -> ClaudeProcess:
        if self.reload_ready.is_set():
            raise ControlError("the runner is restarting; try again in a few seconds")
        if not Path(cwd).is_dir():
            raise ControlError("directory does not exist")
        setup = await self.claude_setup(api)
        session_id = str(uuidlib.uuid4())
        self.prefs.remember(session_id, api=setup.route, browser="1" if browser else None)
        proc = ClaudeProcess(
            self.cfg,
            session_id=session_id,
            cwd=cwd,
            model=model,
            permission_mode=permission_mode,
            effort=effort,
            sink=self._sink,
            tools=self.tool_server,
            extra_tools=self.extra_tools,
            api=setup,
            browser=browser,
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
        route = (self.prefs.get(session_id) or {}).get("api") or "anthropic:subscription"
        setup = await self.claude_setup(route)
        self.prefs.remember(new_id, api=route)
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
            tools=self.tool_server,
            extra_tools=self.extra_tools,
            api=setup,
        )
        self.hosted[new_id] = proc
        try:
            await proc.start()
        except Exception:
            self.hosted.pop(new_id, None)
            raise
        return proc

    # ---- model APIs (routes.py) ---------------------------------------------------

    def session_working(self, session_id: str) -> bool | None:
        """Whether a hosted session of any harness is busy (a turn, queued messages, a prompt, or open background tasks); None: not running."""
        if self.harnesses.has(session_id):
            return self.harnesses.is_working(session_id)
        proc = self.get_hosted(session_id)
        if proc is None or not proc.alive:
            return None
        return bool(proc.busy or proc.queue or proc.permissions or proc.tasks)

    def message_queued(self, session_id: str, message_uuid: str | None) -> bool:
        """Whether `message_uuid` is still queued or held for `session_id` (across any harness or handoff)."""
        if not message_uuid:
            return False
        target = self.final_handoff_target(session_id)
        for sid in (session_id, target):
            if any(h.get("uuid") == message_uuid for h in self._handoff_held.get(sid, ())):
                return True
            if self.delivery is not None and any(h.uuid == message_uuid for h in self.delivery.held.get(sid, ())):
                return True
        if self.harnesses.has(target):
            driver = self.harnesses.drivers.get(target)
            return bool(driver is not None and driver.alive and driver.has_queued(message_uuid))
        proc = self.get_hosted(target)
        return bool(proc is not None and proc.alive and message_uuid in proc.queue)

    def _last_active(self, sid: str, pref: dict[str, Any]) -> float:
        """When a session last did something (its subagent row is "recent" for a while after)."""
        if self.harnesses.has(sid):
            last = (self.harnesses.records.get(sid) or {}).get("updatedAt") or 0
        else:
            entry = self.store.get(sid)
            last = entry.mtime if entry is not None else 0
            proc = self.get_hosted(sid)
            if proc is not None:
                last = max(last, proc.last_activity)
        return max(last, pref.get("time") or 0)

    def child_entries(self, parent: str) -> list[dict[str, Any]]:
        """The sessions started as `parent`'s subagents (tools.py: subagent_start, browser_task), as entries of
        its subagent list. `sessionId` makes the web app open them as sessions (prompts are answered there);
        `via` and `harness` say how they were started and what runs them."""
        out = []
        for sid in self.prefs.children(parent):
            pref = self.prefs.get(sid) or {}
            harness = self.harnesses.harness_of(sid) or "claude"
            running = bool(self.session_working(sid))
            browser = bool(pref.get("browser"))
            out.append({"agentId": f"rz-{sid}", "sessionId": sid, "toolUseId": None,
                        "description": pref.get("subagentTask") or "",
                        "agentType": "浏览器任务" if browser else (HARNESSES[harness].name if harness in HARNESSES else "Claude Code"),
                        "via": "browser" if browser else "riesz", "harness": harness, "background": True,
                        "finished": not running, "running": running, "size": 0, "mtime": self._last_active(sid, pref)})
        if self.harnesses.harness_of(parent) == "antigravity":
            driver = self.harnesses.drivers.get(parent)
            parent_alive = bool(driver is not None and driver.alive)
            out.extend(self.agy_subagents.list(parent, parent_alive=parent_alive))
        return out

    def bins(self) -> dict[str, str | None]:
        return {"claude": self.cfg.claude_bin, "codex": self.harnesses.bin("codex"), "dsh": self.harnesses.bin("dsh"),
                "agy": self.harnesses.bin("antigravity")}

    async def claude_setup(self, route: str | None) -> routes.ClaudeSetup:
        """How to start Claude Code for a route (None: the first available, the subscription before keys)."""
        # Nothing available: try the login anyway (a failed check is not proof of no login; claude says if it is).
        route = route or await routes.default("claude", self.credentials, self.bins()) or "anthropic:subscription"
        try:
            deepseek_url = await self.deepseek_proxy.ensure_started() if route == "deepseek:key" else None
            return routes.claude_setup(route, self.credentials, deepseek_base_url=deepseek_url)
        except CredentialError as exc:
            raise ControlError(str(exc)) from None

    async def set_api(self, session_id: str, route: str, model: str | None = None, *, _internal: bool = False) -> None:
        """Move a Claude Code session to another route; a running process is restarted on it (when idle)."""
        if not routes.valid("claude", route):
            raise ControlError("Claude Code cannot use that API")
        await self.claude_setup(route)
        current_route = (self.prefs.get(session_id) or {}).get("api") or "anthropic:subscription"
        if route == current_route and model is None:
            if not _internal:
                await self.quota_fallback.on_manual_api_change(session_id, route)
                await self.broadcast_global({"ev": "sessions", "sessions": [self.session_summary(session_id)], "removed": []})
                await self.broadcast_session(session_id, {"ev": "state", "state": self.session_state(session_id)})
            return
        proc = self.get_hosted(session_id)
        if proc is not None and proc.working:
            raise ControlError("the session is working; switch when this turn is over")
        if not _internal:
            await self.quota_fallback.on_manual_api_change(session_id, route)
        self.prefs.remember(session_id, api=route)
        if proc is not None:
            self.prefs.forget(session_id, "restoreModel")
            old_pid = proc.pid
            await proc.stop()
            live = self.live.get(session_id)
            if live is not None and live.pid == old_pid:      # not rescanned yet: it was ours
                self.live.pop(session_id, None)
            await self.ensure_process(session_id, model=model)
        elif model and _model_compatible(route, model):
            self.prefs.remember(session_id, restoreModel=model)
        else:
            self.prefs.forget(session_id, "restoreModel")
        await self.broadcast_global({"ev": "sessions", "sessions": [self.session_summary(session_id)], "removed": []})
        await self.broadcast_session(session_id, {"ev": "state", "state": self.session_state(session_id)})

    # ---- continuing in another harness (handoff.py) ----------------------------------

    def _handoff_file(self, session_id: str) -> Path:
        return self.cfg.state_dir / "handoff" / f"{session_id}.json"

    def imported_items(self, session_id: str) -> list[dict[str, Any]]:
        """For a Claude Code session continuing another one: the earlier session's items, shown first."""
        if not (self.prefs.get(session_id) or {}).get("handoffFrom"):
            return []
        try:
            return json.loads(self._handoff_file(session_id).read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            return []

    def display_items(self, session_id: str, own: list[dict[str, Any]]) -> list[dict[str, Any]]:
        imported = self.imported_items(session_id)
        return imported + own if imported else own

    async def _source_items(self, session_id: str) -> list[dict[str, Any]]:
        if self.harnesses.has(session_id):
            return self.harnesses.items(session_id)
        view = await self.catch_up(session_id)
        return self.display_items(session_id, view.items if view is not None else [])

    def final_handoff_target(self, session_id: str) -> str:
        """Follow handedOffTo links to the session that currently carries this conversation on."""
        cur = session_id
        seen = {cur}
        for _ in range(10):
            rec = self.harnesses.records.get(cur) or {}
            nxt = rec.get("handedOffTo") or (self.prefs.get(cur) or {}).get("handedOffTo")
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        return cur

    def handoff_queue_entries(self, session_id: str) -> list[dict[str, Any]]:
        return [{"uuid": h["uuid"], "preview": h["preview"][:300], "state": "held", "ts": h["ts"]}
                for h in self._handoff_held.get(session_id, [])]

    async def hold_during_handoff(self, session_id: str, message_uuid: str, text: str,
                                  attachments: list[dict[str, Any]], options: dict[str, Any]) -> None:
        preview = text.strip() or f"[{len(attachments)} 个附件]"
        self._handoff_held.setdefault(session_id, []).append({
            "uuid": message_uuid, "text": text, "attachments": attachments,
            "preview": preview, "options": options, "ts": time.time(),
        })
        await self.broadcast_session(session_id, {"ev": "state", "state": self.session_state(session_id)})

    async def cancel_handoff_held(self, session_id: str, message_uuid: str) -> bool:
        held = self._handoff_held.get(session_id) or []
        kept = [h for h in held if h["uuid"] != message_uuid]
        if len(kept) == len(held):
            return False
        if kept:
            self._handoff_held[session_id] = kept
        else:
            self._handoff_held.pop(session_id, None)
        await self.broadcast_session(session_id, {"ev": "state", "state": self.session_state(session_id)})
        return True

    async def _compact_claude(self, session_id: str, timeout: float = 300.0) -> None:
        """Have Claude Code compact a session (as /compact would) and wait for it."""
        before = sum(1 for it in await self._source_items(session_id) if it.get("kind") == "compact")
        assert self.delivery is not None
        await self.delivery.send(session_id, "/compact", [], str(uuidlib.uuid4()), _bypass_handoff=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2.0)
            if sum(1 for it in await self._source_items(session_id) if it.get("kind") == "compact") > before:
                return
        raise ControlError("compacting the conversation took too long; try again later")

    def _mode_tier(self, session_id: str, summary: dict[str, Any]) -> str:
        """The tier (modes.py) of the mode a session uses now."""
        from .modes import CLAUDE_TIERS
        if not self.harnesses.has(session_id):
            return CLAUDE_TIERS.get(summary.get("permissionMode") or "default", "ask")
        driver = self.harnesses.drivers.get(session_id)
        rec = self.harnesses.records[session_id]
        listed = driver.modes() if driver is not None and driver.alive else \
            HARNESSES[rec["harness"]].driver(cwd="/", sink=None).modes()
        return next((m.tier for m in listed if m.id == rec.get("mode")), "ask")

    def _closest_mode(self, target: str, tier: str) -> str | None:
        """The mode of `target` its table names for this tier (Claude Code's bypass only where allowed)."""
        from .modes import CLAUDE_MODE_TABLE
        if target != "claude":
            return HARNESSES[target].modes.get(tier)
        mode = CLAUDE_MODE_TABLE.get(tier)
        return CLAUDE_MODE_TABLE["auto"] if mode == "bypassPermissions" and not self.cfg.allow_bypass else mode

    async def handoff(self, session_id: str, target: str, *, api: str | None = None, model: str | None = None,
                      mode: str | None = None, fork: bool = False) -> str:
        """Continue a session in another harness (handoff.py); returns the session that now carries it on.
        `fork`: a copy in the same harness instead, the source going on as it was."""
        from . import handoff
        if target not in handoff.TARGETS:
            raise ControlError(f"a conversation cannot be moved to {target}")
        summary = self.session_summary(session_id)
        source = summary.get("harness") or "claude"
        if source == target and not fork:
            raise ControlError("the session already runs there")
        if mode is None:                                  # the nearest to the mode it had (modes.py tables)
            mode = self._closest_mode(target, self._mode_tier(session_id, summary))
        cwd = summary.get("cwd")
        if not cwd or not Path(cwd).is_dir():
            raise ControlError("the session's working directory no longer exists")
        if not fork:
            if session_id in self._handing_off:
                raise ControlError("the session is already moving to another harness")
            self._handing_off.add(session_id)
        new_id: str | None = None
        try:
            items = await self._source_items(session_id)
            msgs = handoff.conversation(handoff.since_last_compaction(items))
            if handoff.size(msgs) > handoff.BUDGET_CHARS and source == "claude" and not self.harnesses.has(session_id):
                await self._compact_claude(session_id)            # the usual way out of a long conversation
                items = await self._source_items(session_id)
                msgs = handoff.conversation(handoff.since_last_compaction(items))
            if not msgs:
                raise ControlError("the session has no conversation to move")
            record_dir = self.cfg.state_dir / "handoff"
            record_dir.mkdir(parents=True, exist_ok=True)
            record = record_dir / f"{session_id}-{int(time.time())}.md"
            record.write_text(handoff.record_text(handoff.conversation(items)), encoding="utf-8")
            msgs = handoff.fit(msgs, handoff.BUDGET_CHARS, str(record))
            head = handoff.preface(source, summary.get("model"))
            title = summary.get("customTitle") or summary.get("title") or summary.get("firstPrompt")
            shown = handoff.display_items(items, session_id, target, fork=fork)

            if target == "claude":
                setup = await self.claude_setup(api)
                new_id = str(uuidlib.uuid4())
                msgs[0] = {**msgs[0], "text": head + "\n\n" + msgs[0]["text"]}
                records = handoff.claude_records(msgs, new_id, cwd, summary.get("model"), title)
                await asyncio.to_thread(handoff.write_claude_transcript, self.cfg.projects_dir, cwd, records, new_id)
                self._handoff_file(new_id).write_text(json.dumps({"items": shown}, ensure_ascii=False), encoding="utf-8")
                self.store.refresh()
                self.prefs.remember(new_id, api=setup.route, handoffFrom=session_id)
                await self.ensure_process(new_id, model=model, permission_mode=mode)
            elif HARNESSES[target].handoff == "inject-responses":
                driver = await self.harnesses.create(target, cwd, model=model, mode=mode, api=api)
                new_id = str(driver.session_id)
                msgs[0] = {**msgs[0], "text": head + "\n\n" + msgs[0]["text"]}
                await driver.peer.request("thread/inject_items", {"threadId": new_id, "items": handoff.responses_items(msgs)},
                                          timeout=120)
                await self.harnesses.set_imported(new_id, shown, session_id, title)
            else:
                # Nothing to write into: the history goes in front of the first message sent there.
                driver = await self.harnesses.create(target, cwd, model=model, mode=mode, api=api)
                new_id = str(driver.session_id)
                await self.harnesses.set_imported(new_id, shown, session_id, title, pending=handoff.history_text(msgs, head))

            # One session for the user: the source steps aside, stops running, and points to the new one.
            if not fork:
                if self.harnesses.has(session_id):
                    self.harnesses.records[session_id]["handedOffTo"] = new_id
                    self.harnesses._save()
                    await self.harnesses.stop(session_id)
                else:
                    self.prefs.remember(session_id, handedOffTo=new_id)
                    await self.stop_process(session_id)
                await self.auto_resume.taken_over(session_id)
            log.info("moved the conversation of %s (%s) to %s session %s", session_id[:8], source, target, new_id[:8])
            await self.broadcast_global({"ev": "sessions", "sessions": [self.session_summary(new_id), self.session_summary(session_id)],
                                         "removed": []})
        finally:
            if not fork:
                self._handing_off.discard(session_id)
                held = self._handoff_held.pop(session_id, [])
                dest = new_id or session_id
                if held and self.delivery is not None:
                    for h in held:
                        try:
                            await self.delivery.send(dest, h["text"], h["attachments"], h["uuid"])
                        except Exception:  # noqa: BLE001
                            log.exception("failed to forward message held during handoff to %s", dest[:8])
        assert new_id is not None
        return new_id

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
