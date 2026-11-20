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
import re
import shutil
import time
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .drivers.antigravity import AntigravityDriver
from .drivers.base import UNIVERSAL_COMMANDS, Driver, universal_commands
from .modes import TIERS
from .drivers.codex import CodexDriver, fork_thread, probe_models
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

COMMAND_FORMS = re.compile(r"^(/\S+|offered:/\S+|method:\w+|none:.+)$")


@dataclass(frozen=True)
class HarnessSpec:
    """Everything the runner and the web app need to know about a harness. Every field is required, and the
    tables are checked here: adding a harness means deciding each entry (drivers/README.md lists them all,
    tests/test_conformance.py checks what can only be checked once the harness is registered)."""
    id: str
    name: str                 # full name, e.g. "DeepSeek Harness"
    label: str                # short name for tags, e.g. "DeepSeek"
    bin: str                  # executable looked up on PATH, in nvm's Node versions and in ~/.local/bin
    driver: type[Driver]
    arg: str                  # the driver's keyword for the executable's path
    native_provider: str      # the provider of its own route (credentials.PROVIDERS), e.g. "openai" for Codex
    resume_command: str | None   # terminal command to continue a session, "{id}" for its id; None: none by id
    handoff: str              # how a conversation moved here is given to it: one of handoff.STRATEGIES
    fork: str                 # "native" (the harness forks itself) or "copy" (a new session with the history rewritten)
    commands: dict[str, str]  # each of drivers.base.UNIVERSAL_COMMANDS -> how this harness does it (forms listed there)
    modes: dict[str, str]     # each of modes.TIERS -> the mode of this harness to use for it
    tools: str                # Riesz Manager's own tools (tools.py): "mcp" (Driver.mount_tools) or "none:<reason>"

    def __post_init__(self) -> None:
        if set(self.commands) != set(UNIVERSAL_COMMANDS):
            raise TypeError(f"{self.id}: commands must cover exactly {sorted(UNIVERSAL_COMMANDS)}, got {sorted(self.commands)}")
        for name, how in self.commands.items():
            if not COMMAND_FORMS.match(how):
                raise TypeError(f"{self.id}: /{name} -> {how!r} is not one of /cmd, offered:/cmd, method:name, none:reason")
            if how.startswith("method:") and not callable(getattr(self.driver, how[7:], None)):
                raise TypeError(f"{self.id}: /{name} -> {how}: {self.driver.__name__} has no such method")
        if set(self.modes) != set(TIERS):
            raise TypeError(f"{self.id}: modes must name a mode for each of {TIERS}, got {sorted(self.modes)}")
        known = {m.id for m in self.driver(cwd="/", sink=None).modes()}
        if known and not set(self.modes.values()) <= known:
            raise TypeError(f"{self.id}: modes names {sorted(set(self.modes.values()) - known)}, not modes of {self.driver.__name__}")
        from .handoff import STRATEGIES
        if self.handoff not in STRATEGIES:
            raise TypeError(f"{self.id}: handoff {self.handoff!r} is not one of {sorted(STRATEGIES)} (handoff.py)")
        if self.fork not in ("native", "copy"):
            raise TypeError(f"{self.id}: fork must be native or copy")
        if self.tools == "mcp":
            if self.driver.mount_tools is Driver.mount_tools:
                raise TypeError(f"{self.id}: tools='mcp' but {self.driver.__name__} does not implement mount_tools")
        elif not self.tools.startswith("none:") or len(self.tools) <= 5:
            raise TypeError(f"{self.id}: tools must be 'mcp' or 'none:<reason>'")

    def meta(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "label": self.label, "nativeProvider": self.native_provider,
                "resumeCommand": self.resume_command, "handoff": self.handoff, "fork": self.fork, "tools": self.tools}


HARNESSES: dict[str, HarnessSpec] = {s.id: s for s in (
    HarnessSpec(
        id="codex", name="Codex", label="Codex", bin="codex", driver=CodexDriver, arg="codex", native_provider="openai",
        resume_command="codex resume {id}", handoff="inject-responses", fork="native",
        commands={"compact": "method:compact_thread"},                     # thread/compact/start (checked 11-20)
        # No mode asks before every edit: read-only (asks before writing) is the nearest careful one.
        modes={"readonly": "read-only", "ask": "read-only", "edits": "auto", "auto": "auto", "full": "full-access"},
        tools="mcp",
    ),
    HarnessSpec(
        id="dsh", name="DeepSeek Harness", label="DeepSeek", bin="dsh", driver=DshDriver, arg="dsh", native_provider="deepseek",
        resume_command="dsh tui --resume {id}", handoff="first-message", fork="copy",
        commands={"compact": "/compact"},                                   # its SDK runs bare /compact natively
        modes={"readonly": "read-only", "ask": "read-only", "edits": "workspace-write", "auto": "workspace-write",
               "full": "danger-full-access"},
        tools="mcp",
    ),
    HarnessSpec(
        id="gemini", name="Gemini CLI", label="Gemini", bin="gemini", driver=GeminiDriver, arg="gemini", native_provider="gemini",
        resume_command=None, handoff="first-message", fork="copy",
        # Its ACP mode offers only memory, extensions, init, restore, about and help: /compress is not among them.
        commands={"compact": "none:Gemini CLI 在 ACP 模式下没有压缩命令（上下文快满时它会自己压缩）"},
        modes={"readonly": "plan", "ask": "default", "edits": "autoEdit", "auto": "autoEdit", "full": "yolo"},
        tools="mcp",
    ),
    HarnessSpec(
        id="antigravity", name="Antigravity", label="Antigravity", bin="agy", driver=AntigravityDriver, arg="agy",
        native_provider="gemini", resume_command="agy --conversation {id}", handoff="first-message", fork="copy",
        commands={"compact": "offered:/compact"},     # agy has /compact; used when its ACP command list offers it
        # No read-only mode: plan (plans first, then asks like default) is the most careful. Auto needs its
        # terminal sandbox, so accept-edits stands in. Nothing approves everything: Riesz Manager's rz-bypass.
        modes={"readonly": "plan", "ask": "default", "edits": "accept-edits", "auto": "accept-edits", "full": "rz-bypass"},
        tools="mcp",
    ),
)}

# Claude Code runs through process.py rather than a driver; the same facts about it.
CLAUDE_META = {"id": "claude", "name": "Claude Code", "label": "Claude Code", "nativeProvider": "anthropic",
               "resumeCommand": "claude --resume {id}", "handoff": "claude", "fork": "native", "tools": "mcp"}
CLAUDE_COMMANDS = {"compact": "/compact"}       # Claude Code's own; its whole command list comes from the process


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
        return find_bin(HARNESSES[harness].bin, configured or None)

    def commands(self, session_id: str | None = None, harness: str | None = None) -> list[dict[str, Any]]:
        """The composer's slash commands for a session (or a harness, before one exists): the universal commands
        the harness can do (HarnessSpec.commands), then its own."""
        harness = harness or self.harness_of(session_id or "")
        spec = HARNESSES[harness]
        driver = self.drivers.get(session_id or "")
        own = driver.native_commands() if driver is not None and driver.alive else []
        offered = {c["name"] for c in own}
        out = []
        for cmd in universal_commands():
            how = spec.commands[cmd["name"]]
            if how.startswith("none:") or (how.startswith("offered:") and driver is not None and driver.alive
                                           and how.split("/", 1)[1] not in offered):
                continue
            out.append(cmd)
        return out + [c for c in own if c["name"] not in UNIVERSAL_COMMANDS]

    async def run_universal(self, session_id: str, name: str, args: str, message_uuid: str) -> None:
        """A universal command, the way HarnessSpec.commands says this harness does it."""
        rec = self.records[session_id]
        spec = HARNESSES[rec["harness"]]
        how = spec.commands[name]
        if how.startswith("none:"):
            raise ControlError(how[5:])
        driver = await self.ensure(session_id)
        try:
            if how.startswith("method:"):
                await getattr(driver, how[7:])(args)
                return
            native = how.split(":", 1)[1] if how.startswith("offered:") else how
            if how.startswith("offered:") and native[1:] not in {c["name"] for c in driver.native_commands()}:
                raise ControlError(f"{spec.name} does not offer {native} in this session")
            await driver.send(native + (f" {args}" if args else ""), [], message_uuid)
        except (ValueError, JsonRpcError) as exc:
            raise ControlError(getattr(exc, "message", None) or str(exc)) from None

    async def available(self) -> list[dict[str, Any]]:
        """Every harness (Claude Code first): what it is (HarnessSpec.meta), its routes, modes and commands,
        and whether a session of it can be started."""
        bins = self.hub.bins()
        out: list[dict[str, Any]] = [{**CLAUDE_META, "available": True, "reason": None,
                                      "routes": await routes.describe("claude", self.hub.credentials, bins)}]
        for hid, spec in HARNESSES.items():
            path = self.bin(hid)
            sample = spec.driver(cwd="/", sink=_nowhere)
            described = await routes.describe(hid, self.hub.credentials, bins) if path else []
            reason = None
            if path is None:
                reason = f"{spec.bin} 没有安装"
            elif hid in routes.ROUTES and not any(r["available"] for r in described):   # (test harnesses have no routes)
                reason = described[0]["reason"] if len(described) == 1 else "没有可用的订阅或 API key"
            out.append({**spec.meta(), "available": reason is None, "reason": reason, "routes": described,
                        "modes": [m.__dict__ for m in sample.modes()], "defaultMode": sample.mode,
                        "defaultModel": sample.model, "capabilities": spec.driver.capabilities.as_dict(),
                        "commands": self.commands(harness=hid)})
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
                raise ControlError(f"{HARNESSES[harness].name} cannot use {route}")
            return route
        found = await routes.default(harness, self.hub.credentials, self.hub.bins())
        if found is None:
            raise ControlError(f"no subscription or API key is set up for {HARNESSES[harness].name}")
        return found

    async def _env(self, harness: str, route: str) -> dict[str, str]:
        env = ClaudeProcess._child_env()
        path = self.bin(harness)
        if path is None:
            raise ControlError(f"{HARNESSES[harness].bin} is not installed")
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

    async def set_imported(self, session_id: str, items: list[dict[str, Any]], source: str, title: str | None,
                           pending: str | None = None) -> None:
        """`pending`: history to put in front of the next message (harnesses with no history to write into)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{session_id}.imported.json").write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
        rec = self.records[session_id]
        rec["handoffFrom"] = source
        rec["promptCount"] = max(rec.get("promptCount") or 0, 1)        # listed like a session with a conversation
        if pending:
            rec["pendingHandoff"] = pending
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
            "live": self.live_info(session_id), "mtime": rec.get("updatedAt") or 0, "size": 0, "keepAlive": bool(rec.get("keepAlive")),
            "startedBy": (self.hub.prefs.get(session_id) or {}).get("startedBy"), "interrupted": self.hub.prefs.interrupted(session_id), "subagents": 0,
            "api": rec.get("api"), "handoffFrom": rec.get("handoffFrom"), "handedOffTo": rec.get("handedOffTo"),
        }

    def summaries(self, project: str | None = None) -> list[dict[str, Any]]:
        out = [self.summary(sid) for sid, rec in self.records.items() if rec.get("promptCount") or sid in self.drivers]
        return [s for s in out if not project or s.get("project") == project]

    def state(self, session_id: str) -> dict[str, Any]:
        driver = self.drivers.get(session_id)
        rec = self.records[session_id]
        cls = HARNESSES[rec["harness"]].driver
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
        return self._imported(session_id) + _shown(own)

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
        extra: dict[str, Any] = {spec.arg: self.bin(harness)}
        model = model or await self._default_model(harness, route)
        if harness in ("codex", "antigravity"):
            extra["api"] = route
        elif harness == "dsh":
            extra["provider"] = routes.dsh_provider(route)
            extra["patch"] = await self._patch(harness, session_id, route)
        elif harness == "gemini":
            extra["settings"] = settings_file(self.cfg.state_dir)
        if spec.tools == "mcp":
            extra["tools"] = self.hub.tool_server
        return spec.driver(cwd=cwd, sink=self._sink, session_id=session_id, model=model, effort=effort, mode=mode,
                              env=await self._env(harness, route), **extra)

    async def create(self, harness: str, cwd: str, *, model: str | None = None, effort: str | None = None,
                     mode: str | None = None, api: str | None = None, session_id: str | None = None) -> Driver:
        if harness not in HARNESSES:
            raise ControlError(f"unknown harness {harness}")
        if self.hub.reload_ready.is_set():
            raise ControlError("the runner is restarting; try again in a few seconds")
        if not Path(cwd).is_dir():
            raise ControlError("directory does not exist")
        route = await self._route(harness, api)
        if session_id is None and harness not in ("codex", "gemini", "antigravity"):   # those name their sessions
            session_id = str(uuidlib.uuid4())
        driver = await self._make(harness, cwd=cwd, session_id=session_id, route=route, model=model, effort=effort, mode=mode)
        try:
            await driver.start()
        except JsonRpcError as exc:                 # the harness refused (not signed in, ...): say why
            raise ControlError(f"{HARNESSES[harness].name}: {exc.message}") from None
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
                raise ControlError(f"{HARNESSES[rec['harness']].name}: {exc.message}") from None
            except Exception:
                self.drivers.pop(session_id, None)
                raise
            self._active[session_id] = time.time()
            log.info("resumed %s session %s", rec["harness"], session_id[:8])
            await self.hub.broadcast_global({"ev": "session_live", "sessionId": session_id, "live": self.live_info(session_id)})
            return driver

    async def send(self, session_id: str, text: str, attachments: list[dict[str, Any]], message_uuid: str, *,
                   model: str | None = None, mode: str | None = None, effort: str | None = None) -> dict[str, Any]:
        """Send a message, starting the session if needed; `model`, `mode`, `effort`: chosen while it was not running.
        A universal slash command (UNIVERSAL_COMMANDS) is run the harness's own way instead."""
        stripped = text.strip()
        if stripped.startswith("/") and not attachments:
            name, _, args = stripped[1:].partition(" ")
            if name in UNIVERSAL_COMMANDS:
                await self.run_universal(session_id, name, args.strip(), message_uuid)
                return {"uuid": message_uuid, "held": False}
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
        if rec.get("pendingHandoff") and not stripped.startswith("/"):
            # The conversation moved here from another harness, put in front of the first message
            # (not of a slash command, which the harness would no longer take as one).
            text = rec.pop("pendingHandoff") + "\n\n" + text
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

    async def fork(self, session_id: str) -> str:
        """A new session continuing from this one, which goes on as it was (HarnessSpec.fork says how)."""
        rec = self.records[session_id]
        spec = HARNESSES[rec["harness"]]
        if spec.fork != "native":
            return await self.hub.handoff(session_id, rec["harness"], fork=True)
        route = rec.get("api") or await self._route(rec["harness"], None)
        try:
            new_id = await fork_thread(self.bin(rec["harness"]) or spec.bin, rec["cwd"], await self._env(rec["harness"], route),
                                       session_id, route)
        except JsonRpcError as exc:
            raise ControlError(f"{spec.name}: {exc.message}") from None
        now = time.time()
        self.records[new_id] = {**{k: rec.get(k) for k in ("harness", "cwd", "api", "model", "effort", "mode", "customTitle",
                                                           "title", "firstPrompt", "lastPrompt", "promptCount", "handoffFrom")},
                                "id": new_id, "createdAt": now, "updatedAt": now, "forkedFrom": session_id}
        imported = self.cache_dir / f"{session_id}.imported.json"
        if imported.is_file():                         # the conversation it continued, shown first as before
            (self.cache_dir / f"{new_id}.imported.json").write_bytes(imported.read_bytes())
        self._save()
        await self.ensure(new_id)
        await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.summary(new_id)], "removed": []})
        return new_id

    async def set_keep_alive(self, session_id: str, on: bool) -> None:
        """Keep the session's process running: not stopped when idle, started again when it ends."""
        rec = self.records[session_id]
        rec["keepAlive"] = on
        self._save()
        if on:
            await self.ensure(session_id)
        await self.hub.broadcast_global({"ev": "sessions", "sessions": [self.summary(session_id)], "removed": []})

    async def set_api(self, session_id: str, route: str, model: str | None = None) -> None:
        """Move a session to another route: its process restarts on it (when idle) and picks the history up."""
        rec = self.records[session_id]
        route = await self._route(rec["harness"], route)
        if route != rec.get("api") and not HARNESSES[rec["harness"]].driver.capabilities.api_switch:
            # Codex keeps each thread on the provider it started with.
            raise ControlError(f"a {HARNESSES[rec['harness']].name} session stays on the API it started with; start a new session")
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

    async def stop(self, session_id: str, by_user: bool = False) -> bool:
        """`by_user`: stopping it by hand also ends "keep running" (it would come straight back)."""
        if by_user and self.records.get(session_id, {}).get("keepAlive"):
            self.records[session_id]["keepAlive"] = False
            self._save()
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
        """Stop drivers idle for the runner's idle timeout (like Claude Code processes); start the ones kept running."""
        failures: dict[str, tuple[float, int]] = {}
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            now = time.time()
            for sid, driver in list(self.drivers.items()):
                rec = self.records.get(sid) or {}
                if not driver.alive or driver.busy or driver.permissions or driver.tasks or rec.get("keepAlive"):
                    continue
                if now - self._active.get(sid, now) > self.cfg.idle_timeout:
                    log.info("stopping idle %s session %s", driver.harness, sid[:8])
                    with contextlib.suppress(Exception):
                        await self.stop(sid)
            if self.hub.reload_ready.is_set():
                continue
            for sid, rec in list(self.records.items()):
                driver = self.drivers.get(sid)
                if not rec.get("keepAlive") or rec.get("handedOffTo") or (driver is not None and driver.alive):
                    continue
                last, count = failures.get(sid, (0.0, 0))
                if now - last < min(60 * 2 ** count, 1800):
                    continue                            # it keeps failing: back off
                try:
                    await self.ensure(sid)
                    failures.pop(sid, None)
                    log.info("keep-alive: started %s session %s", rec["harness"], sid[:8])
                except Exception as exc:  # noqa: BLE001
                    failures[sid] = (now, count + 1)
                    log.info("keep-alive: could not start %s: %s", sid[:8], exc)

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
            if rec is not None and rec.get("handoffFrom"):
                shown = _shown(list(event.get("items") or []))
                event = {**event, "items": self._imported(sid) + shown if ev == "reset" else shown}
        if ev == "rate_limit" and driver.harness == "codex" and getattr(driver, "api", None) in (None, "openai:subscription"):
            self.hub.limits.codex_update(event.get("info"))          # the ChatGPT plan's limits, for the doll
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


def _shown(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items as the web app shows them: history put in front of a message for a handoff is left out."""
    from .handoff import PREFACE_START, strip_history
    out = []
    for it in items:
        if it.get("kind") == "user" and str(it.get("text") or "").startswith(PREFACE_START):
            text = strip_history(it["text"])
            if not text:
                continue
            it = {**it, "text": text}
        out.append(it)
    return out


async def _nowhere(driver: Driver, event: dict[str, Any]) -> None:
    return None
