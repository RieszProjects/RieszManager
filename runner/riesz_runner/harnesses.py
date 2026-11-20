"""Sessions of harnesses other than Claude Code (Codex, DeepSeek Harness), run through drivers/.

The rest of the runner was built around Claude Code: its transcripts, its
process protocol. Sessions of other harnesses live beside that instead of
inside it: this module keeps their list (<state dir>/harness_sessions.json),
the last conversation of each (<state dir>/harness/<id>.json, shown while the
session is not running), and one running driver per active session. The web
app reaches them through the same endpoints as Claude Code sessions; the
server hands requests for these sessions to the functions here.

Session ids are UUIDs, so they fit everywhere a Claude Code session id does:
Codex's own thread ids are, and dsh sessions are given one.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import logging
import os
import shutil
import time
import uuid as uuidlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .drivers.antigravity import AntigravityDriver
from .drivers.base import Driver
from .drivers.codex import CodexDriver, probe_models
from .drivers.dsh import EFFORTS, DshDriver
from .drivers.gemini import GeminiDriver, settings_file
from .drivers.jsonrpc import JsonRpcError
from . import routes
from .attention import LONG_TURN_SECONDS, describe_request
from .credentials import PROVIDERS, CredentialError
from .process import ClaudeProcess, ControlError

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

SAVE_DELAY = 2.0
REAP_INTERVAL = 60.0

MODELS_TTL = 600.0

HARNESSES: dict[str, dict[str, Any]] = {
    # "arg": the driver's keyword for the executable's path.
    "codex": {"name": "Codex", "bin": "codex", "arg": "codex", "driver": CodexDriver},
    "dsh": {"name": "DeepSeek Harness", "bin": "dsh", "arg": "dsh", "driver": DshDriver},
    "gemini": {"name": "Gemini CLI", "bin": "gemini", "arg": "gemini", "driver": GeminiDriver},
    "antigravity": {"name": "Antigravity", "bin": "agy", "arg": "agy", "driver": AntigravityDriver},
}


def find_bin(name: str, configured: str | None) -> str | None:
    """The harness's executable: as configured, on PATH, or in an nvm Node installation (newest first)."""
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    found = shutil.which(name) or (os.path.expanduser(f"~/.local/bin/{name}") if os.access(os.path.expanduser(f"~/.local/bin/{name}"), os.X_OK) else None)
    if found:
        return found
    candidates = sorted(glob.glob(os.path.expanduser(f"~/.nvm/versions/node/*/bin/{name}")), key=os.path.getmtime, reverse=True)
    return candidates[0] if candidates else None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"


class Harnesses:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.cfg = hub.cfg
        self.path = self.cfg.state_dir / "harness_sessions.json"
        self.cache_dir = self.cfg.state_dir / "harness"
        self.records: dict[str, dict[str, Any]] = {}
        self.drivers: dict[str, Driver] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._save_handles: dict[str, asyncio.TimerHandle] = {}
        self._active: dict[str, float] = {}
        self._models: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._stopping: set[str] = set()
        self._load()

    # ---- what is available ------------------------------------------------------------

    def bin(self, harness: str) -> str | None:
        configured = {"codex": self.cfg.codex_bin, "dsh": self.cfg.dsh_bin, "gemini": self.cfg.gemini_bin,
                      "antigravity": self.cfg.agy_bin}.get(harness)
        return find_bin(HARNESSES[harness]["bin"], configured or None)

    async def available(self) -> list[dict[str, Any]]:
        """Every harness (Claude Code first) with its routes, and whether a session of it can be started."""
        bins = self.hub.bins()
        out: list[dict[str, Any]] = [{"id": "claude", "name": "Claude Code", "available": True, "reason": None,
                                      "routes": await routes.describe("claude", self.hub.credentials, bins)}]
        for hid, spec in HARNESSES.items():
            path = self.bin(hid)
            driver_cls = spec["driver"]
            sample = driver_cls(cwd="/", sink=_nowhere)
            described = await routes.describe(hid, self.hub.credentials, bins) if path and hid in routes.ROUTES else []
            reason = None
            if path is None:
                reason = f"{spec['bin']} 没有安装"
            elif hid in routes.ROUTES and not any(r["available"] for r in described):
                reason = described[0]["reason"] if len(described) == 1 else "没有可用的订阅或 API key"
            out.append({"id": hid, "name": spec["name"], "available": reason is None, "reason": reason, "routes": described,
                        "modes": [m.__dict__ for m in sample.modes()], "defaultMode": sample.mode,
                        "defaultModel": sample.model, "capabilities": driver_cls.capabilities.as_dict()})
        if not any(r["available"] for r in out[0]["routes"]):
            out[0]["available"] = False
            out[0]["reason"] = "没有登录 claude.ai，也没有设置 API key"
        return out

    async def _route(self, harness: str, route: str | None) -> str:
        """The route a session uses: the one asked for, else the first available (subscription first)."""
        if harness not in routes.ROUTES:
            return route or ""
        if route:
            if not routes.valid(harness, route):
                raise ControlError(f"{HARNESSES[harness]['name']} cannot use {route}")
            return route
        found = await routes.default(harness, self.hub.credentials, self.hub.bins())
        if found is None:
            raise ControlError(f"no subscription or API key is set up for {HARNESSES[harness]['name']}")
        return found

    async def _env(self, harness: str, route: str) -> dict[str, str]:
        env = ClaudeProcess._child_env()
        path = self.bin(harness)
        if path is None:
            raise ControlError(f"{HARNESSES[harness]['bin']} is not installed")
        env["PATH"] = os.path.dirname(path) + os.pathsep + env.get("PATH", "")   # node lives next to it
        for name in (p.env for p in PROVIDERS.values()):
            env.pop(name, None)                       # only the key the route needs
        if not route:
            return env
        provider, auth = routes.split(route)
        if auth == "key":
            try:
                env[PROVIDERS[provider].env] = await self.hub.credentials.key(provider)
            except CredentialError as exc:
                raise ControlError(str(exc)) from None
        if harness == "dsh" and provider != "deepseek" and self.hub.credentials.configured("deepseek"):
            with contextlib.suppress(CredentialError):   # dsh's web search goes through DeepSeek
                env["DEEPSEEK_API_KEY"] = await self.hub.credentials.key("deepseek")
        return env

    async def _default_model(self, harness: str, route: str) -> str | None:
        """A model for a route whose harness would otherwise pick one of another provider's."""
        provider = routes.split(route)[0] if route else None
        if not ((harness == "codex" and provider == "deepseek") or (harness == "dsh" and provider not in (None, "deepseek"))):
            return None
        try:
            models = [m["value"] for m in await self.hub.credentials.models(str(provider))]
        except CredentialError as exc:
            raise ControlError(str(exc)) from None
        if harness == "codex":                 # DeepSeek's Responses API serves its Flash model
            return next((m for m in models if "flash" in m), "deepseek-flash")
        return models[0] if models else None

    async def _patch(self, harness: str, session_id: str | None, route: str) -> str | None:
        """dsh: the overlay that mounts the route's provider, written next to the session's cached items."""
        if harness != "dsh" or session_id is None or not route:
            return None
        provider = routes.split(route)[0]
        if provider == "deepseek":
            return None
        try:
            models = await self.hub.credentials.models(provider)
        except CredentialError as exc:
            raise ControlError(str(exc)) from None
        text = routes.dsh_patch(route, models)
        if text is None:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{session_id}.dsh-patch.yml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    # ---- records -------------------------------------------------------------------------

    def _load(self) -> None:
        with contextlib.suppress(FileNotFoundError, ValueError, OSError):
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for rec in data.get("sessions", []):
                if isinstance(rec, dict) and rec.get("id") and rec.get("harness") in HARNESSES:
                    self.records[rec["id"]] = rec

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": list(self.records.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def has(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self.records

    def harness_of(self, session_id: str) -> str | None:
        rec = self.records.get(session_id)
        return rec["harness"] if rec else None

    # ---- a conversation moved here from another session (handoff.py) -------------------

    def _imported(self, session_id: str) -> list[dict[str, Any]]:
        """The earlier conversation shown before this session's own items (its harness does not list it)."""
        if not (self.records.get(session_id) or {}).get("handoffFrom"):
            return []
        with contextlib.suppress(FileNotFoundError, ValueError, OSError):
            return json.loads((self.cache_dir / f"{session_id}.imported.json").read_text(encoding="utf-8")).get("items", [])
        return []

    async def set_imported(self, session_id: str, items: list[dict[str, Any]], source: str, title: str | None) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{session_id}.imported.json").write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
        rec = self.records[session_id]
        rec["handoffFrom"] = source
        if title:
            rec["customTitle"] = title
        self._save()
        await self.hub.broadcast_session(session_id, {"ev": "reset", "items": self.items(session_id), "hasMore": False})

    def _cached_items(self, session_id: str) -> list[dict[str, Any]]:
        with contextlib.suppress(FileNotFoundError, ValueError, OSError):
            return json.loads((self.cache_dir / f"{session_id}.json").read_text(encoding="utf-8")).get("items", [])
        return []

    def _save_items_soon(self, session_id: str) -> None:
        handle = self._save_handles.pop(session_id, None)
        if handle:
            handle.cancel()
        self._save_handles[session_id] = asyncio.get_running_loop().call_later(SAVE_DELAY, self._save_items, session_id)

    def _save_items(self, session_id: str) -> None:
        self._save_handles.pop(session_id, None)
        driver = self.drivers.get(session_id)
        if driver is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{session_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": driver.items.items}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # ---- what the web app reads ----------------------------------------------------------

    def live_info(self, session_id: str) -> dict[str, Any] | None:
        driver = self.drivers.get(session_id)
        if driver is None or not driver.alive:
            return None
        return {"hosted": True, "busy": driver.busy, "pid": driver.pid, "waiting": bool(driver.permissions),
                "agents": [], "harness": driver.harness}

    def summary(self, session_id: str) -> dict[str, Any]:
        rec = self.records[session_id]
        return {
            "sessionId": session_id, "harness": rec["harness"], "cwd": rec.get("cwd"), "gitBranch": None,
            "title": rec.get("customTitle") or rec.get("title"), "customTitle": rec.get("customTitle"), "firstPrompt": rec.get("firstPrompt"),
            "lastPrompt": rec.get("lastPrompt"), "createdAt": _iso(rec.get("createdAt") or 0), "updatedAt": _iso(rec.get("updatedAt") or 0),
            "promptCount": rec.get("promptCount") or 0, "model": rec.get("model"), "effort": rec.get("effort"),
            "permissionMode": rec.get("mode"), "project": self.hub.projects.project_id_for(rec.get("cwd")),
            "live": self.live_info(session_id), "mtime": rec.get("updatedAt") or 0, "size": 0, "keepAlive": False,
            "startedBy": None, "interrupted": self.hub.prefs.interrupted(session_id), "subagents": 0,
            "api": rec.get("api"), "handoffFrom": rec.get("handoffFrom"),
        }

    def summaries(self, project: str | None = None) -> list[dict[str, Any]]:
        out = [self.summary(sid) for sid, rec in self.records.items() if rec.get("promptCount") or sid in self.drivers]
        return [s for s in out if not project or s.get("project") == project]

    def state(self, session_id: str) -> dict[str, Any]:
        driver = self.drivers.get(session_id)
        rec = self.records[session_id]
        cls = HARNESSES[rec["harness"]]["driver"]
        if driver is not None and driver.alive:
            state = driver.snapshot()
        else:
            state = {"harness": rec["harness"], "hosted": False, "alive": False, "busy": False, "queue": [], "permissions": [],
                     "partial": None, "model": rec.get("model"), "effort": rec.get("effort"), "permissionMode": rec.get("mode"),
                     "capabilities": cls.capabilities.as_dict(), "exitCode": driver.exit_code if driver else None,
                     "lastResult": driver.last_result if driver else None}
        sample = driver or cls(cwd="/", sink=_nowhere)
        state["modes"] = [m.__dict__ for m in sample.modes()]
        state["api"] = rec.get("api")
        return state

    def items(self, session_id: str) -> list[dict[str, Any]]:
        driver = self.drivers.get(session_id)
        own = driver.items.items if driver is not None and driver.items.items else self._cached_items(session_id)
        return self._imported(session_id) + own

    async def models(self, session_id: str | None = None, harness: str | None = None,
                     route: str | None = None) -> list[dict[str, Any]]:
        """The models a harness offers on a route: Codex's own list for OpenAI, the provider's list otherwise."""
        rec = self.records.get(session_id or "")
        harness = harness or (rec["harness"] if rec else None)
        if harness not in HARNESSES:
            return []
        if harness not in routes.ROUTES:       # a harness without routes: whatever its driver offers
            driver = self.drivers.get(session_id or "")
            return await driver.models() if driver is not None and driver.alive else []
        route = route or (rec.get("api") if rec else None) or await routes.default(harness, self.hub.credentials, self.hub.bins())
        if route is None or not routes.valid(harness, route):
            return []
        provider, auth = routes.split(route)
        try:
            if auth == "subscription" and harness == "antigravity":
                driver = self.drivers.get(session_id or "")
                if driver is not None and driver.alive and rec and rec.get("api") == route and await driver.models():
                    return await driver.models()
                login = await self.hub.credentials.subscription(provider, self.hub.bins())
                return list((login or {}).get("models") or [])
            if harness == "codex" and provider == "openai":
                cached = self._models.get(f"codex:{route}")
                if cached and time.time() - cached[0] < MODELS_TTL:
                    return cached[1]
                driver = self.drivers.get(session_id or "")
                if driver is not None and driver.alive and rec and rec.get("api") == route:
                    models = await driver.models()
                else:
                    models = await probe_models(self.bin("codex") or "codex", str(self.cfg.workspace_root),
                                                await self._env("codex", route))
                if models:
                    self._models[f"codex:{route}"] = (time.time(), models)
                return models
            models = await self.hub.credentials.models(provider)
        except CredentialError as exc:
            raise ControlError(str(exc)) from None
        if harness == "dsh" and provider == "deepseek":
            # dsh adds "off" to DeepSeek's effort levels.
            models = [{**m, "supportedEffortLevels": ["off", *(m.get("supportedEffortLevels") or EFFORTS[1:])]} for m in models]
        return models

    # ---- sessions --------------------------------------------------------------------------

    async def _make(self, harness: str, *, cwd: str, session_id: str | None, route: str, model: str | None,
                    effort: str | None, mode: str | None) -> Driver:
        spec = HARNESSES[harness]
        extra: dict[str, Any] = {spec["arg"]: self.bin(harness)} if spec.get("arg") else {}
        model = model or await self._default_model(harness, route)
        if harness in ("codex", "antigravity"):
            extra["api"] = route
        elif harness == "dsh":
            extra["provider"] = routes.dsh_provider(route)
            extra["patch"] = await self._patch(harness, session_id, route)
        elif harness == "gemini":
            extra["settings"] = settings_file(self.cfg.state_dir)
        return spec["driver"](cwd=cwd, sink=self._sink, session_id=session_id, model=model, effort=effort, mode=mode,
                              env=await self._env(harness, route), **extra)

    async def create(self, harness: str, cwd: str, *, model: str | None = None, effort: str | None = None,
                     mode: str | None = None, api: str | None = None) -> Driver:
        if harness not in HARNESSES:
            raise ControlError(f"unknown harness {harness}")
        if self.hub.reload_ready.is_set():
            raise ControlError("the runner is restarting; try again in a few seconds")
        if not Path(cwd).is_dir():
            raise ControlError("directory does not exist")
        route = await self._route(harness, api)
        session_id = None if harness in ("codex", "gemini", "antigravity") else str(uuidlib.uuid4())   # these name their sessions
        driver = await self._make(harness, cwd=cwd, session_id=session_id, route=route, model=model, effort=effort, mode=mode)
        try:
            await driver.start()
        except JsonRpcError as exc:                 # the harness refused (not signed in, ...): say why
            raise ControlError(f"{HARNESSES[harness]['name']}: {exc.message}") from None
        sid = str(driver.session_id)
        now = time.time()
        self.records[sid] = {"id": sid, "harness": harness, "cwd": cwd, "createdAt": now, "updatedAt": now, "promptCount": 0,
                             "model": driver.model, "effort": driver.effort, "mode": driver.mode, "api": route}
        self.drivers[sid] = driver
        self._active[sid] = now
        self._save()
        log.info("started %s session %s in %s", harness, sid[:8], cwd)
        return driver

    async def ensure(self, session_id: str) -> Driver:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            driver = self.drivers.get(session_id)
            if driver is not None and driver.alive:
                return driver
            if self.hub.reload_ready.is_set():
                raise ControlError("the runner is restarting; try again in a few seconds")
            rec = self.records.get(session_id)
            if rec is None:
                raise ControlError("unknown session")
            if not Path(rec.get("cwd") or "").is_dir():
                raise ControlError("the session's working directory no longer exists")
            route = rec.get("api") or await self._route(rec["harness"], None)
            rec["api"] = route
            driver = await self._make(rec["harness"], cwd=rec["cwd"], session_id=session_id, route=route,
                                      model=rec.get("model"), effort=rec.get("effort"), mode=rec.get("mode"))
            self.drivers[session_id] = driver
            try:
                await driver.start()
            except JsonRpcError as exc:             # the harness refused: say why
                self.drivers.pop(session_id, None)
                raise ControlError(f"{HARNESSES[rec['harness']]['name']}: {exc.message}") from None
            except Exception:
                self.drivers.pop(session_id, None)
                raise
            self._active[session_id] = time.time()
            log.info("resumed %s session %s", rec["harness"], session_id[:8])
            await self.hub.broadcast_global({"ev": "session_live", "sessionId": session_id, "live": self.live_info(session_id)})
            return driver

    async def send(self, session_id: str, text: str, attachments: list[dict[str, Any]], message_uuid: str, *,
                   model: str | None = None, mode: str | None = None, effort: str | None = None) -> dict[str, Any]:
        """Send a message, starting the session if needed; `model`, `mode`, `effort`: chosen while it was not running."""
        running = self.drivers.get(session_id)
        if running is None or not running.alive:
            rec = self.records[session_id]
            for key, value in (("model", model), ("mode", mode), ("effort", effort)):
                if value:
                    rec[key] = value
        driver = await self.ensure(session_id)
        images = [{"path": a["path"], "mime": a.get("mime")} for a in attachments if str(a.get("mime") or "").startswith("image/")]
        files = [a["path"] for a in attachments if not str(a.get("mime") or "").startswith("image/")]
        if files:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + "\n".join(f"附件：{p}" for p in files)
        rec = self.records[session_id]
        rec["promptCount"] = (rec.get("promptCount") or 0) + 1
        rec["firstPrompt"] = rec.get("firstPrompt") or text[:200]
        rec["lastPrompt"] = text[:200]
        rec["updatedAt"] = time.time()
        self._active[session_id] = time.time()
        self._save()
        await driver.send(text, images, message_uuid)
        await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.summary(session_id)], "removed": []})
        return {"uuid": message_uuid, "held": False}

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        driver = self.drivers.get(session_id)
        if driver is None or not driver.alive:
            raise ControlError("session is not running here")
        await driver.interrupt()
        return {"interrupted": True}

    def answer(self, session_id: str, request_id: str, body: dict[str, Any]) -> bool:
        driver = self.drivers.get(session_id)
        if driver is None:
            return False
        pending = driver.permissions.get(request_id)
        if pending is None:
            return False
        allow = body.get("behavior") == "allow"
        scope = "session" if any(isinstance(p, dict) and p.get("type") == "session" for p in body.get("updatedPermissions") or ()) \
            or body.get("scope") == "session" else "once"
        answers: dict[str, Any] = {}
        updated = body.get("updatedInput") if isinstance(body.get("updatedInput"), dict) else {}
        if isinstance(updated.get("answers"), dict):
            # The web app answers AskUserQuestion by question text ("a, b"); drivers want ids and lists.
            by_text = {str(q.get("question")): q.get("id") for q in (pending.entry.get("input") or {}).get("questions") or ()}
            for question, value in updated["answers"].items():
                qid = by_text.get(str(question), question)
                answers[qid] = [v.strip() for v in str(value).split(", ") if v.strip()]
        self._active[session_id] = time.time()
        return driver.answer_permission(request_id, allow=allow, scope=scope, message=body.get("message"), answers=answers)

    async def set_api(self, session_id: str, route: str, model: str | None = None) -> None:
        """Move a session to another route: its process restarts on it (when idle) and picks the history up."""
        rec = self.records[session_id]
        route = await self._route(rec["harness"], route)
        if route != rec.get("api") and not HARNESSES[rec["harness"]]["driver"].capabilities.api_switch:
            # Codex keeps each thread on the provider it started with.
            raise ControlError(f"a {HARNESSES[rec['harness']]['name']} session stays on the API it started with; start a new session")
        driver = self.drivers.get(session_id)
        if driver is not None and driver.alive and (driver.busy or driver.permissions or driver.tasks):
            raise ControlError("the session is working; switch when this turn is over")
        running = driver is not None and driver.alive
        if running:
            await self.stop(session_id)        # first: its last state report would bring the old model back
        rec["api"] = route
        rec["model"] = model                   # None: the route's default model
        self._save()
        if running:
            await self.ensure(session_id)
        await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.summary(session_id)], "removed": []})
        await self.hub.broadcast_session(session_id, {"ev": "state", "state": self.state(session_id)})

    async def set(self, session_id: str, what: str, value: Any) -> Driver:
        driver = await self.ensure(session_id)
        if what == "model":
            await driver.set_model(value)
        elif what == "effort":
            await driver.set_effort(value)
        elif what == "mode":
            await driver.set_mode(value)
        rec = self.records[session_id]
        rec.update(model=driver.model, effort=driver.effort, mode=driver.mode)
        self._save()
        return driver

    async def rename(self, session_id: str, title: str) -> None:
        self.records[session_id]["customTitle"] = title
        self._save()
        await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.summary(session_id)], "removed": []})

    async def stop(self, session_id: str) -> bool:
        driver = self.drivers.get(session_id)
        if driver is None or not driver.alive:
            return False
        self._save_items(session_id)
        self._stopping.add(session_id)
        try:
            await driver.stop()
        finally:
            self._stopping.discard(session_id)
        await self.hub.broadcast_global({"ev": "session_live", "sessionId": session_id, "live": None})
        return True

    def working_ids(self) -> list[str]:
        return [sid for sid, d in self.drivers.items() if d.alive and (d.busy or d.permissions or d.tasks)]

    def working(self) -> bool:
        return bool(self.working_ids())

    async def close(self) -> None:
        for sid in list(self.drivers):
            self._save_items(sid)
        await asyncio.gather(*(d.stop() for d in self.drivers.values() if d.alive), return_exceptions=True)

    async def loop(self) -> None:
        """Stop drivers that have been idle for the runner's idle timeout, like Claude Code processes."""
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            now = time.time()
            for sid, driver in list(self.drivers.items()):
                if not driver.alive or driver.busy or driver.permissions or driver.tasks:
                    continue
                if now - self._active.get(sid, now) > self.cfg.idle_timeout:
                    log.info("stopping idle %s session %s", driver.harness, sid[:8])
                    with contextlib.suppress(Exception):
                        await self.stop(sid)

    # ---- what drivers report ------------------------------------------------------------

    async def _sink(self, driver: Driver, event: dict[str, Any]) -> None:
        sid = driver.session_id
        if not sid:
            return
        ev = event.get("ev")
        rec = self.records.get(sid)
        if ev in ("items", "reset"):
            if rec is not None:
                self._save_items_soon(sid)
            if ev == "reset" and rec is not None and rec.get("handoffFrom"):
                event = {**event, "items": self._imported(sid) + list(event.get("items") or [])}
        if ev == "state":
            event = {"ev": "state", "state": self.state(sid) if rec else driver.snapshot()}
            if rec is not None:
                changed = False
                for key, value in (("model", driver.model), ("effort", driver.effort), ("mode", driver.mode)):
                    if value and rec.get(key) != value:
                        rec[key] = value
                        changed = True
                if driver.title and rec.get("title") != driver.title:
                    rec["title"] = driver.title
                    changed = True
                if changed:
                    self._save()
        if ev in ("result", "permission", "permission_resolved"):
            self._active[sid] = time.time()
        if ev == "result" and rec is not None:
            rec["updatedAt"] = time.time()
            self._save()
        if rec is not None and ev in ("permission", "result", "exit"):
            await self._attention(sid, event)
        if ev not in ("permission", "permission_resolved", "rate_limit"):
            await self.hub.broadcast_session(sid, event)
        if ev in ("state", "exit") and rec is not None:
            await self.hub.broadcast_global({"ev": "session_live", "sessionId": sid, "live": self.live_info(sid)})
        if ev in ("result", "exit") and rec is not None:
            await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.summary(sid)], "removed": []})

    async def _attention(self, sid: str, event: dict[str, Any]) -> None:
        ev = event.get("ev")
        if ev == "permission":
            kind, detail = describe_request(event.get("request") or {})
            await self.hub._attention(sid, kind, detail=detail)
        elif ev == "result":
            result = event.get("result") or {}
            seconds = (result.get("durationMs") or 0) / 1000
            if seconds >= LONG_TURN_SECONDS:
                await self.hub._attention(sid, "done", seconds=round(seconds), isError=bool(result.get("isError")),
                                          detail=" ".join(str(event.get("text") or "").split())[:160])
        elif ev == "exit" and event.get("code") not in (0, None) and sid not in self._stopping:
            await self.hub._attention(sid, "exited", code=event.get("code"))


async def _nowhere(driver: Driver, event: dict[str, Any]) -> None:
    return None
