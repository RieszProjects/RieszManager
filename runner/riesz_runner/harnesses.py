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

from .drivers.base import Driver
from .drivers.codex import CodexDriver, probe_models
from .drivers.dsh import DshDriver
from .attention import LONG_TURN_SECONDS, describe_request
from .process import ClaudeProcess, ControlError

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

SAVE_DELAY = 2.0
REAP_INTERVAL = 60.0
KEY_TIMEOUT = 20.0

MODELS_TTL = 600.0

HARNESSES: dict[str, dict[str, Any]] = {
    # "arg": the driver's keyword for the executable's path.
    "codex": {"name": "Codex", "bin": "codex", "arg": "codex", "driver": CodexDriver},
    "dsh": {"name": "DeepSeek Harness", "bin": "dsh", "arg": "dsh", "driver": DshDriver},
}


def find_bin(name: str, configured: str | None) -> str | None:
    """The harness's executable: as configured, on PATH, or in an nvm Node installation (newest first)."""
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    found = shutil.which(name)
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

    def _bin(self, harness: str) -> str | None:
        configured = {"codex": self.cfg.codex_bin, "dsh": self.cfg.dsh_bin}.get(harness)
        return find_bin(HARNESSES[harness]["bin"], configured or None)

    def _key_command(self) -> str | None:
        """How to get the DeepSeek API key: RUNNER_DSH_KEY_COMMAND, else the balance checker's command."""
        if self.cfg.dsh_key_command:
            return self.cfg.dsh_key_command
        with contextlib.suppress(Exception):
            for p in json.loads(self.cfg.balances_file.read_text(encoding="utf-8")).get("providers", []):
                if p.get("type") == "deepseek" and p.get("keyCommand"):
                    return str(p["keyCommand"])
        return None

    def available(self) -> list[dict[str, Any]]:
        out = []
        for hid, spec in HARNESSES.items():
            path = self._bin(hid)
            reason = None if path else f"{spec['bin']} 没有安装"
            if path and hid == "dsh" and not self._key_command():
                reason = "没有配置 DeepSeek API key 的获取方式"
            driver_cls = spec["driver"]
            sample = driver_cls(cwd="/", sink=_nowhere)
            out.append({"id": hid, "name": spec["name"], "available": reason is None, "reason": reason,
                        "modes": [m.__dict__ for m in sample.modes()], "defaultMode": sample.mode,
                        "defaultModel": sample.model, "capabilities": driver_cls.capabilities.as_dict()})
        return out

    async def _env(self, harness: str) -> dict[str, str]:
        env = ClaudeProcess._child_env()
        path = self._bin(harness)
        if path is None:
            raise ControlError(f"{HARNESSES[harness]['bin']} is not installed")
        env["PATH"] = os.path.dirname(path) + os.pathsep + env.get("PATH", "")   # node lives next to it
        if harness == "dsh":
            command = self._key_command()
            if not command:
                raise ControlError("no command is configured to get the DeepSeek API key (RUNNER_DSH_KEY_COMMAND)")
            env["DEEPSEEK_API_KEY"] = await self._run_key(command, env)
        return env

    async def _run_key(self, command: str, env: dict[str, str]) -> str:
        shell = os.environ.get("SHELL") or "/bin/bash"
        args = [shell, "-lc", command] if self.cfg.login_shell else ["/bin/sh", "-c", command]
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, env=env)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), KEY_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise ControlError("the API key command timed out") from None
        lines = [line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()]
        if proc.returncode != 0 or not lines:
            raise ControlError(f"the API key command failed (exit {proc.returncode})")
        return lines[-1]

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
            "title": rec.get("title"), "customTitle": rec.get("customTitle"), "firstPrompt": rec.get("firstPrompt"),
            "lastPrompt": rec.get("lastPrompt"), "createdAt": _iso(rec.get("createdAt") or 0), "updatedAt": _iso(rec.get("updatedAt") or 0),
            "promptCount": rec.get("promptCount") or 0, "model": rec.get("model"), "effort": rec.get("effort"),
            "permissionMode": rec.get("mode"), "project": self.hub.projects.project_id_for(rec.get("cwd")),
            "live": self.live_info(session_id), "mtime": rec.get("updatedAt") or 0, "size": 0, "keepAlive": False,
            "startedBy": None, "interrupted": self.hub.prefs.interrupted(session_id), "subagents": 0,
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
        return state

    def items(self, session_id: str) -> list[dict[str, Any]]:
        driver = self.drivers.get(session_id)
        if driver is not None and driver.items.items:
            return driver.items.items
        return self._cached_items(session_id)

    async def models(self, session_id: str | None = None, harness: str | None = None) -> list[dict[str, Any]]:
        """The models a harness offers (Codex asks its app-server, dsh asks DeepSeek's API); kept a few minutes."""
        harness = harness or (self.harness_of(session_id) if session_id else None)
        if harness not in HARNESSES:
            return []
        cached = self._models.get(harness)
        if cached and time.time() - cached[0] < MODELS_TTL:
            return cached[1]
        driver = self.drivers.get(session_id or "")
        if driver is not None and driver.alive:
            models = await driver.models()
        elif harness == "codex":
            models = await probe_models(self._bin("codex") or "codex", str(self.cfg.workspace_root), await self._env("codex"))
        else:
            models = await DshDriver(cwd=str(self.cfg.workspace_root), sink=_nowhere, env=await self._env("dsh")).models()
        if models:
            self._models[harness] = (time.time(), models)
        return models

    # ---- sessions --------------------------------------------------------------------------

    def _make(self, harness: str, *, cwd: str, env: dict[str, str], session_id: str | None, model: str | None,
              effort: str | None, mode: str | None) -> Driver:
        spec = HARNESSES[harness]
        extra = {spec["arg"]: self._bin(harness)} if spec.get("arg") else {}
        return spec["driver"](cwd=cwd, sink=self._sink, session_id=session_id, model=model, effort=effort, mode=mode, env=env, **extra)

    async def create(self, harness: str, cwd: str, *, model: str | None = None, effort: str | None = None,
                     mode: str | None = None) -> Driver:
        if harness not in HARNESSES:
            raise ControlError(f"unknown harness {harness}")
        if self.hub.reload_ready.is_set():
            raise ControlError("the runner is restarting; try again in a few seconds")
        if not Path(cwd).is_dir():
            raise ControlError("directory does not exist")
        session_id = None if harness == "codex" else str(uuidlib.uuid4())   # Codex names its threads itself
        driver = self._make(harness, cwd=cwd, env=await self._env(harness), session_id=session_id,
                            model=model, effort=effort, mode=mode)
        await driver.start()
        sid = str(driver.session_id)
        now = time.time()
        self.records[sid] = {"id": sid, "harness": harness, "cwd": cwd, "createdAt": now, "updatedAt": now, "promptCount": 0,
                             "model": driver.model, "effort": driver.effort, "mode": driver.mode}
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
            driver = self._make(rec["harness"], cwd=rec["cwd"], env=await self._env(rec["harness"]), session_id=session_id,
                                model=rec.get("model"), effort=rec.get("effort"), mode=rec.get("mode"))
            self.drivers[session_id] = driver
            try:
                await driver.start()
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
