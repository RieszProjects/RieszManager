"""HTTP + WebSocket API of the runner (served on a Unix domain socket)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import mimetypes
import os
import re
import time
import urllib.parse
import uuid as uuidlib
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import files as files_mod
from . import shares as shares_mod
from .claude_settings import user_defaults
from .config import Config
from .context_limits import ContextLimits, limits_from_usage
from .delivery import Delivery
from . import routes
from .credentials import PROVIDERS, SUBSCRIPTIONS, CredentialError
from .harnesses import HARNESSES
from .history import PromptHistory
from .hub import Hub
from .probe import CapabilityProbe
from .process import ControlError
from .schedules import ScheduleError, system_timezone
from .nodes import MAX_TRANSFER_BYTES, NodeError, build_client_zip, render_bootstrap_py
from .notes import NoteError
from . import tools as tools_mod
from .projects import ROOT_ID
from .transcript import resolve_image
from .uploads import UploadStore

log = logging.getLogger(__name__)

SNAPSHOT_ITEMS = 160
PAGE_ITEMS = 160
WS_QUEUE_LIMIT = 5000
PERMISSION_MODES = {"default", "acceptEdits", "plan", "auto", "bypassPermissions", "dontAsk", "manual"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

HUB = web.AppKey("hub", Hub)
TOOL_SESSION = web.RequestKey("toolSession", str)      # the hosted session calling Riesz Manager's tools
CFG = web.AppKey("cfg", Config)
UPLOADS = web.AppKey("uploads", UploadStore)
HISTORY = web.AppKey("history", PromptHistory)
PROBE = web.AppKey("probe", CapabilityProbe)
DELIVERY = web.AppKey("delivery", Delivery)
LIMITS = web.AppKey("limits", ContextLimits)
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _err(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    cfg = request.app[CFG]
    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied.encode(), cfg.token.encode()):
        # Riesz Manager's tools (tools.py) are called by the tool server a hosted session's harness started:
        # the session is the one that process descends from, and no token is needed. The list itself is
        # the same for everyone (and is asked for while a harness starts, before its session is known).
        pid = tools_mod.peer_pid(request.transport) if request.path in ("/v1/tools", "/v1/tools/call") else None
        if pid is None:
            return _err(401, "unauthorized")
        if request.path == "/v1/tools/call":
            caller = tools_mod.caller_session(request.app[HUB], pid)
            if caller is None:
                return _err(401, "Riesz Manager 的工具只能在它托管的会话里使用")
            request[TOOL_SESSION] = caller
    try:
        return await handler(request)
    except ControlError as exc:
        return _err(409, str(exc))
    except files_mod.FileAccessError as exc:
        return _err(404, str(exc))


def _session_id(request: web.Request) -> str:
    sid = request.match_info["sid"]
    if not _UUID_RE.match(sid):
        raise web.HTTPBadRequest(text="invalid session id")
    return sid


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="invalid json")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="invalid json")
    return data


# ---- projects & sessions -----------------------------------------------------

async def health(request: web.Request) -> web.Response:
    cfg = request.app[CFG]
    return _json({"ok": True, "workspaceRoot": str(cfg.workspace_root), "claudeBin": cfg.claude_bin,
                  "allowBypass": cfg.allow_bypass,
                  "defaultPermissionMode": cfg.default_permission_mode, "time": time.time()})


async def plan_usage(request: web.Request) -> web.Response:
    """Subscription usage windows (see usage.py). `?refresh=1` bypasses the cache."""
    force = request.query.get("refresh") in ("1", "true")
    return _json(await request.app[HUB].usage.get(force=force))


async def balances(request: web.Request) -> web.Response:
    """Balances of other AI services (see balances.py). `?refresh=1` checks again unless checked in the last minute."""
    force = request.query.get("refresh") in ("1", "true")
    return _json(await request.app[HUB].balances.get(force=force))


async def subscription_limits(request: web.Request) -> web.Response:
    """Usage limits of the other subscriptions (see limits.py). `?refresh=1` checks again unless checked in the last minute."""
    force = request.query.get("refresh") in ("1", "true")
    return _json(await request.app[HUB].limits.get(force=force))


MAX_PROJECT_NAME_BYTES = 200


def _project_name_problem(name: str) -> str | None:
    """Why `name` cannot be a new project folder, or None. The codes are mapped to messages by the web app."""
    if not name:
        return "empty"
    if name in (".", "..") or name.startswith("."):
        return "dot"            # hidden folders are not listed as projects
    if "/" in name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "chars"
    if len(name.encode()) > MAX_PROJECT_NAME_BYTES:
        return "long"
    return None


async def create_project(request: web.Request) -> web.Response:
    """Create a folder directly under the workspace root; it becomes a project."""
    hub = request.app[HUB]
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    problem = _project_name_problem(name)
    if problem:
        return _json({"error": "invalid project name", "code": problem}, status=400)
    root = hub.cfg.workspace_root
    if not root.is_dir():
        return _json({"error": "the workspace root does not exist", "code": "no-root"}, status=409)
    path = root / name
    try:
        path.mkdir()
    except FileExistsError:
        return _json({"error": "a file or folder with this name already exists", "code": "exists"}, status=409)
    except OSError as exc:
        return _json({"error": f"cannot create the folder: {exc.strerror}", "code": "os"}, status=409)
    log.info("created project folder %s", path)
    project = hub.projects.describe(name, path)
    project.update(sessionCount=0, lastActivity=project.get("mtime"), liveCount=0)
    return _json({"project": project})


async def list_projects(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    hub.store.refresh()
    projects: dict[str, dict[str, Any]] = {p["id"]: p for p in hub.projects.workspace_projects()}
    for p in projects.values():
        p.update(sessionCount=0, lastActivity=p.get("mtime"), liveCount=0)
    for sid, entry in hub.store.entries.items():
        cwd = entry.scanner.meta.cwd
        pid = hub.projects.project_id_for(cwd)
        if pid is None:
            continue
        proj = projects.get(pid)
        if proj is None:
            path = hub.projects.path_for(pid)
            if path is None:
                continue
            proj = hub.projects.describe(pid, path)
            proj.update(sessionCount=0, lastActivity=None, liveCount=0)
            projects[pid] = proj
        proj["sessionCount"] += 1
        if entry.mtime and (proj["lastActivity"] or 0) < entry.mtime:
            proj["lastActivity"] = entry.mtime
        if hub._live_info(sid):
            proj["liveCount"] += 1
    ordered = sorted(projects.values(), key=lambda p: (p["external"], -(p["lastActivity"] or 0)))
    return _json({"projects": ordered, "workspaceRoot": str(hub.cfg.workspace_root)})


async def project_detail(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    pid = request.match_info["pid"]
    path = hub.projects.path_for(pid)
    if path is None:
        return _err(404, "unknown project")
    info = hub.projects.describe(pid, path)
    if info["filesAllowed"]:
        from .projects import git_status
        info["git"] = await asyncio.to_thread(git_status, path)
    return _json({"project": info})


async def list_sessions(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    hub.store.refresh()
    project = request.query.get("project")
    limit = min(int(request.query.get("limit", "500")), 2000)
    sessions = []
    for sid, entry in hub.store.entries.items():
        if entry.scanner.meta.prompt_count == 0 and not hub._live_info(sid):
            continue  # empty shells (e.g. sessions that never got a prompt)
        summary = hub.session_summary(sid)
        if project and summary.get("project") != project:
            continue
        sessions.append(summary)
    sessions.extend(hub.harnesses.summaries(project))
    sessions.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
    return _json({"sessions": sessions[:limit]})


async def live_sessions(request: web.Request) -> web.Response:
    return _json({"live": request.app[HUB].live_list()})


async def session_detail(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    if hub.store.get(sid) is None and hub.get_hosted(sid) is None:
        return _err(404, "unknown session")
    return _json({"session": hub.session_summary(sid), "state": hub.session_state(sid)})


async def session_subagents(request: web.Request) -> web.Response:
    """The session's subagents (see subagents.py): agentId, the Agent tool call that started it, ..."""
    hub = request.app[HUB]
    sid = _session_id(request)
    return _json({"subagents": hub.subagents.list(sid) + hub.child_entries(sid)})


async def subagent_items(request: web.Request) -> web.Response:
    """A page of a subagent's display items, newest last (`before` = a seq, `limit`)."""
    hub = request.app[HUB]
    result = await hub.subagents.items(_session_id(request), request.match_info["aid"])
    if result is None:
        return _err(404, "unknown subagent")
    items, mtime = result
    before = request.query.get("before")
    limit = min(int(request.query.get("limit", str(PAGE_ITEMS))), 1000)
    if before is not None:
        b = int(before)
        items = [it for it in items if it["seq"] < b]
    page = items[-limit:]
    return _json({"items": page, "hasMore": len(items) > len(page), "mtime": mtime})


async def stop_task(request: web.Request) -> web.Response:
    """Stop one task (a subagent or background command) of a session running here."""
    proc = request.app[HUB].get_hosted(_session_id(request))
    if proc is None or not proc.alive:
        return _err(409, "session is not running here")
    await proc.stop_task(request.match_info["tid"])
    return _json({"ok": True})


async def background_task(request: web.Request) -> web.Response:
    """Move a foreground task to the background (body: toolUseId of the tool call that runs it)."""
    proc = request.app[HUB].get_hosted(_session_id(request))
    if proc is None or not proc.alive:
        return _err(409, "session is not running here")
    body = await _body(request)
    tool_use_id = body.get("toolUseId")
    if not isinstance(tool_use_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tool_use_id):
        return _err(400, "invalid toolUseId")
    return _json({"backgrounded": await proc.background_task(tool_use_id)})


async def session_items(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    view = await hub.catch_up(sid)
    if view is None:
        return _json({"items": [], "hasMore": False, "total": 0})
    before = request.query.get("before")
    limit = min(int(request.query.get("limit", str(PAGE_ITEMS))), 1000)
    every = hub.display_items(sid, view.items)        # with the earlier session's items when it continues one
    items = every
    if before is not None:
        b = int(before)
        items = [it for it in items if it["seq"] < b]
    page = items[-limit:]
    return _json({"items": page, "hasMore": len(items) > len(page), "total": len(every)})


def _serve_image(hub: Hub, sid: str, ref: str) -> web.StreamResponse:
    """Serve an image from either a session's upload (`file:`), its own transcript (`<off>:<path>`),
    or an earlier session in its handoff chain (`sid:<owner>:<ref>` or fallback search)."""
    if ref.startswith("file:"):
        path = Path(os.path.realpath(ref[5:]))
        if not path.is_relative_to(Path(os.path.realpath(hub.cfg.uploads_dir))) or not path.is_file():
            return _err(404, "not found")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            return _err(404, "not found")
        return web.FileResponse(path, headers={"Content-Type": mime, "Cache-Control": "private, max-age=86400"})

    chain = _handoff_chain(hub, sid)
    targets = chain
    if ref.startswith("sid:"):
        parts = ref.split(":", 2)
        if len(parts) != 3 or parts[1] not in chain or not parts[2]:
            return _err(404, "not found")
        owner, inner = parts[1], parts[2]
        if inner.startswith("file:"):
            return _serve_image(hub, owner, inner)
        targets = [owner]
        ref = inner

    offset_s, _, path = ref.partition(":")
    if not offset_s.isdigit() or not path:
        return _err(400, "invalid ref")
    offset = int(offset_s)
    for cand in targets:
        line = hub.store.read_line_at(cand, offset)
        if not line:
            continue
        resolved = resolve_image(line, path)
        if resolved is not None:
            data, mime = resolved
            return web.Response(body=data, content_type=mime, headers={"Cache-Control": "private, max-age=86400"})
    return _err(404, "not found")


async def session_image(request: web.Request) -> web.StreamResponse:
    return _serve_image(request.app[HUB], _session_id(request), request.query.get("ref", ""))


async def session_commands(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    probe = request.app[PROBE]
    sid = request.match_info.get("sid")
    proc = hub.get_hosted(sid) if sid else None
    if proc is not None and proc.init_info and (proc.api is None or proc.api.route.startswith("anthropic:")):
        models = proc.models()
        return _json({"commands": proc.commands(), "models": models, "source": "session",
                      "defaults": user_defaults(hub.cfg.claude_home, models),
                      "routes": await _routes_with_models(hub, "claude", models)})
    cwd = None
    if sid:
        entry = hub.store.get(sid)
        cwd = entry.scanner.meta.cwd if entry else None
    if not cwd:
        project = request.query.get("project")
        path = hub.projects.path_for(project) if project else None
        cwd = str(path) if path else str(hub.cfg.workspace_root)
    if not Path(cwd).is_dir():
        cwd = str(hub.cfg.workspace_root)
    info = await probe.get(cwd)
    found = await _routes_with_models(hub, "claude", info["models"])
    route = (hub.prefs.get(sid) or {}).get("api") if sid else None
    current = next((r for r in found if r["id"] == route), None)
    models = current["models"] if current is not None and current["provider"] != "anthropic" else info["models"]
    commands = proc.commands() if proc is not None and proc.init_info else info["commands"]
    return _json({"commands": commands, "models": models, "source": "probe",
                  "defaults": user_defaults(hub.cfg.claude_home, info["models"]), "routes": found})


async def _routes_with_models(hub: Hub, harness: str, claude_models: list[dict[str, Any]] | None = None,
                              session_id: str | None = None) -> list[dict[str, Any]]:
    """A harness's routes, each with the models it offers (fetched together; a failing one lists none)."""
    described = await routes.describe(harness, hub.credentials, hub.bins())

    async def models_for(route: dict[str, Any]) -> list[dict[str, Any]]:
        if not route["available"]:
            return []
        if harness == "claude":
            if route["provider"] == "anthropic":
                return claude_models or []
            return await hub.credentials.models(route["provider"])
        return await hub.harnesses.models(session_id, harness=harness, route=route["id"])

    found = await asyncio.gather(*(models_for(r) for r in described), return_exceptions=True)
    for route, models in zip(described, found):
        if isinstance(models, BaseException):
            route["models"], route["error"] = [], str(models) or type(models).__name__
        else:
            route["models"] = models
    return described


async def context_usage(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    proc = hub.get_hosted(_session_id(request))
    if proc is None:
        return _err(409, "session is not running here")
    return _json({"context": await proc.control("get_context_usage")})


async def context_limits(request: web.Request) -> web.Response:
    """Context window and auto-compact point for a session (for the composer's context meter).

    A session running here answers exactly (and also reports its current size);
    otherwise the model's defaults are used, the model being `?model=` or the one
    the transcript last used.
    """
    hub = request.app[HUB]
    sid = _session_id(request)
    proc = hub.get_hosted(sid)
    if proc is not None and proc.alive:
        try:
            usage = await proc.control("get_context_usage", timeout=8)
            return _json({**limits_from_usage(usage), "liveTokens": usage.get("totalTokens"), "source": "live"})
        except (ControlError, asyncio.TimeoutError):
            pass
    model = request.query.get("model") or hub.session_summary(sid).get("model")
    if model == "<synthetic>":
        model = None
    try:
        limits = await request.app[LIMITS].get(model)
    except Exception as exc:  # probe failed: the meter simply stays hidden
        return _err(503, f"context window unknown: {exc}")
    return _json({**limits, "model": model, "source": "model"})


# ---- actions -----------------------------------------------------------------

def _attachments(request: web.Request, ids: Any) -> list[dict[str, Any]]:
    store = request.app[UPLOADS]
    metas = []
    for upload_id in ids or []:
        meta = store.get(str(upload_id))
        if meta is None:
            raise web.HTTPBadRequest(text=f"unknown attachment {upload_id}")
        metas.append(meta)
    return metas


def _validate_mode(cfg: Config, mode: Any) -> str | None:
    if mode in (None, ""):
        return None
    if mode not in PERMISSION_MODES:
        raise web.HTTPBadRequest(text="invalid permission mode")
    if mode == "bypassPermissions" and not cfg.allow_bypass:
        raise web.HTTPForbidden(text="bypassPermissions is disabled on this host")
    return mode


def _validate_effort(effort: Any) -> str | None:
    if effort in (None, ""):
        return None
    if effort not in EFFORT_LEVELS:
        raise web.HTTPBadRequest(text="invalid effort level")
    return str(effort)


def _prompt(request: web.Request, body: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """Validate a prompt body: returns (text, attachment metas, message uuid)."""
    text = str(body.get("text") or "")
    attachments = _attachments(request, body.get("attachments"))
    if not text.strip() and not attachments:
        raise web.HTTPBadRequest(text="empty message")
    message_uuid = body.get("uuid") if _UUID_RE.match(str(body.get("uuid") or "")) else str(uuidlib.uuid4())
    return text, attachments, message_uuid


async def _deliver_new(request: web.Request, proc: Any, body: dict[str, Any]) -> str:
    text, attachments, message_uuid = _prompt(request, body)
    await request.app[DELIVERY].send_new(proc, text, attachments, message_uuid)
    return message_uuid


async def create_session(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    # Either a project id (the web) or a working directory (`rieszctl session new` from another session).
    cwd = body.get("cwd")
    if cwd:
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return _err(400, "cwd must be an absolute path")
        path = Path(os.path.realpath(cwd))
    else:
        project = body.get("project")
        path = hub.projects.path_for(project) if project else None
    if path is None:
        return _err(400, "unknown project")
    started_by = body.get("startedBy")
    if started_by is not None and not (isinstance(started_by, str) and _UUID_RE.match(started_by)):
        return _err(400, "invalid startedBy")
    harness = body.get("harness")
    if harness and harness != "claude":
        return await _create_harness_session(request, str(path), str(harness), body)
    mode = _validate_mode(hub.cfg, body.get("permissionMode"))
    api = body.get("api") or None
    if api is not None and not routes.valid("claude", api):
        return _err(400, "invalid api")
    proc = await hub.create_session(str(path), model=body.get("model") or None, permission_mode=mode,
                                    effort=_validate_effort(body.get("effort")), api=api)
    request.app[PROBE].remember(proc.cwd, _probe_info(proc))
    if started_by:
        hub.prefs.remember(proc.session_id, startedBy=started_by)
    message_uuid = None
    if body.get("text") or body.get("attachments"):
        message_uuid = await _deliver_new(request, proc, body)
    if body.get("keepAlive"):
        await hub.set_keep_alive(proc.session_id, True)
    return _json({"sessionId": proc.session_id, "uuid": message_uuid, "state": proc.snapshot()})


async def _create_harness_session(request: web.Request, cwd: str, harness: str, body: dict[str, Any]) -> web.Response:
    hub = request.app[HUB]
    if harness not in HARNESSES:
        return _err(400, "unknown harness")
    model = body.get("model") or None
    if model is not None and not (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model)):
        return _err(400, "invalid model")
    mode = body.get("permissionMode") or None
    if mode is not None and mode not in {m.id for m in HARNESSES[harness].driver(cwd="/", sink=None).modes()}:
        return _err(400, "invalid permission mode")
    api = body.get("api") or None
    if api is not None and not routes.valid(harness, api):
        return _err(400, "invalid api")
    driver = await hub.harnesses.create(harness, cwd, model=model, effort=_harness_effort(body.get("effort")), mode=mode, api=api)
    sid = str(driver.session_id)
    if body.get("startedBy"):
        hub.prefs.remember(sid, startedBy=body["startedBy"])
    message_uuid = None
    if body.get("text") or body.get("attachments"):
        text, attachments, message_uuid = _prompt(request, body)
        await request.app[DELIVERY].send(sid, text, attachments, message_uuid)
    if body.get("keepAlive"):
        await hub.harnesses.set_keep_alive(sid, True)
    return _json({"sessionId": sid, "uuid": message_uuid, "state": hub.harnesses.state(sid)})


def _probe_info(proc: Any) -> dict[str, Any]:
    info = proc.init_info or {}
    return {"commands": info.get("commands") or [], "models": info.get("models") or [],
            "agents": info.get("agents") or [], "permissionMode": info.get("current_permission_mode")}


async def send_message(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request)
    mode = _validate_mode(hub.cfg, body.get("permissionMode"))
    text, attachments, message_uuid = _prompt(request, body)
    if request.headers.get("X-Riesz-From") == "web":
        await hub.auto_resume.taken_over(sid)
    result = await request.app[DELIVERY].send(
        sid, text, attachments, message_uuid,
        model=body.get("model") or None, permission_mode=mode, effort=_validate_effort(body.get("effort")),
    )
    return _json({**result, "state": hub.session_state(sid)})


def _schedule_view(hub: Any, s: dict[str, Any]) -> dict[str, Any]:
    from .schedules import describe
    out = {k: v for k, v in s.items() if k not in ("lastUuid", "nextBase", "lastBase")}
    out["rule"] = describe(s)
    return out


async def list_tools(request: web.Request) -> web.Response:
    return _json(tools_mod.listing())


async def call_tool(request: web.Request) -> web.Response:
    body = await _body(request)
    sid = request.get(TOOL_SESSION) or body.get("sessionId")       # a token-holder (tests, rieszctl) names the session
    if not isinstance(sid, str) or not _UUID_RE.match(sid):
        return _err(400, "which session is calling is unknown")
    return _json(await tools_mod.run(request.app[HUB], sid, str(body.get("name") or ""), body.get("arguments") or {}))


async def list_schedules(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = request.query.get("session")
    return _json({"schedules": [_schedule_view(hub, s) for s in hub.schedules.list(sid or None)],
                  "timezone": system_timezone()})


async def create_schedule(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    try:
        s = await hub.schedules.create(body, created_by=body.get("createdBy") if isinstance(body.get("createdBy"), str) else None)
    except ScheduleError as exc:
        return _err(400, str(exc))
    return _json({"schedule": _schedule_view(hub, s)})


async def schedule_action(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sched_id = request.match_info["id"]
    action = request.match_info["action"]
    try:
        if action == "update":
            s = await hub.schedules.update(sched_id, await _body(request))
        elif action == "run":
            s = await hub.schedules.run_now(sched_id)
        elif action == "delete":
            await hub.schedules.delete(sched_id)
            return _json({"ok": True})
        else:
            return _err(404, "unknown action")
    except ScheduleError as exc:
        return _err(400, str(exc))
    return _json({"schedule": _schedule_view(hub, s)})


async def list_notes(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = request.query.get("session")
    ids = set(_handoff_chain(hub, sid)) if sid else None
    return _json({"notes": hub.notes.list(ids)})


async def create_note(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    sid = str(body.get("sessionId") or "")
    try:
        note, created = await hub.notes.upsert(
            sid,
            str(body.get("content") or ""),
            title=body.get("title") if body.get("title") is not None else None,
            note_id=str(body["id"]) if body.get("id") else None,
            color=str(body["color"]) if body.get("color") else None,
        )
    except NoteError as exc:
        return _err(400, str(exc))
    return _json({"note": note, "created": created})


async def note_action(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    note_id = request.match_info["id"]
    action = request.match_info["action"]
    try:
        if action == "update":
            note = await hub.notes.update(note_id, await _body(request))
            return _json({"note": note})
        if action == "delete":
            await hub.notes.delete(note_id)
            return _json({"ok": True})
        return _err(404, "unknown action")
    except NoteError as exc:
        return _err(400, str(exc))


async def clear_notes(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    sid = str(body.get("sessionId") or "")
    ids = set(_handoff_chain(hub, sid)) if sid else None
    removed = await hub.notes.clear(ids)
    return _json({"ok": True, "removed": removed})


def _handoff_chain(hub: Hub, sid: str) -> list[str]:
    """The session and the ones it continues (handoffFrom), newest first."""
    chain = [sid]
    while len(chain) < 10:
        rec = hub.harnesses.records.get(chain[-1]) or {}
        prev = rec.get("handoffFrom") or (hub.prefs.get(chain[-1]) or {}).get("handoffFrom")
        if not prev or prev in chain:
            break
        chain.append(prev)
    return chain


async def session_shared(request: web.Request) -> web.Response:
    """Files the session's agent sent to the user (send_file), and those of the sessions it continues."""
    hub = request.app[HUB]
    sid = _session_id(request)
    ws_root = Path(os.path.realpath(hub.cfg.workspace_root))
    out: list[dict[str, Any]] = []
    for s in _handoff_chain(hub, sid):
        for item in hub.shares.list(s):
            orig = item.get("path")
            if orig:
                real = Path(os.path.realpath(orig))
                if real.is_file():
                    try:
                        rel = real.relative_to(ws_root)
                        if len(rel.parts) > 1:
                            item = {**item, "workspaceProject": rel.parts[0], "workspacePath": "/".join(rel.parts[1:])}
                        elif rel.parts:
                            item = {**item, "workspaceProject": ROOT_ID, "workspacePath": rel.parts[0]}
                    except ValueError:
                        pass
            out.append(item)
    return _json({"files": out})


async def open_file(request: web.Request) -> web.Response:
    """Where the web app's file viewer finds a file a reply links to: {"project", "path"}.
    Files in the workspace open in place; files the session's harness wrote for the user in its own
    folders (Driver.artifact_dirs) are copied to the session's sent files; anything else is refused."""
    hub = request.app[HUB]
    sid = _session_id(request)
    raw = str((await _body(request)).get("path") or "")
    if raw.startswith("file://"):
        raw = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
    if not raw.startswith("/"):
        return _err(400, "an absolute path is needed")
    path = Path(os.path.realpath(raw))
    if not path.is_file():
        return _err(404, "文件不存在")
    try:
        rel = path.relative_to(hub.cfg.workspace_root)
        if len(rel.parts) > 1:
            return _json({"project": rel.parts[0], "path": "/".join(rel.parts[1:])})
        return _json({"project": ROOT_ID, "path": rel.parts[0]})
    except ValueError:
        pass
    rec = hub.harnesses.records.get(sid)
    if rec is not None:
        for template in HARNESSES[rec["harness"]].driver.artifact_dirs:
            folder = Path(os.path.realpath(os.path.expanduser(template.format(id=sid))))
            if folder in path.parents:
                entry = hub.shares.share(sid, str(path))
                return _json({"project": entry["project"], "path": entry["ref"]})
    if hub.cfg.allow_external_files:
        return _json({"project": hub.projects.project_id_for(str(path.parent)), "path": path.name})
    return _err(403, "这个文件不在工作区里，网页打不开；可以让它用 send_file 把文件发给你")


RESUME_TEXT = ("刚才服务器重启（或运行环境意外停止），你上一轮的工作被打断了，当时在跑的命令和后台任务也都停了。"
               "请先看看中断前在做什么、做到了哪一步，再接着完成。")


async def resume_session(request: web.Request) -> web.Response:
    """Pick up an interrupted session: ask Claude to carry on from where it was cut off."""
    hub = request.app[HUB]
    sid = _session_id(request)
    if request.headers.get("X-Riesz-From") == "web":
        await hub.auto_resume.taken_over(sid)
    result = await request.app[DELIVERY].send(sid, RESUME_TEXT, [], str(uuidlib.uuid4()))
    return _json({**result, "state": hub.session_state(sid)})


async def dismiss_interrupted(request: web.Request) -> web.Response:
    await request.app[HUB].clear_interrupted(_session_id(request))
    return _json({"ok": True})


async def cancel_auto_resume(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    cancelled = await hub.auto_resume.taken_over(sid)
    return _json({"cancelled": cancelled, "state": hub.session_state(sid)})


async def start_session(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request) if request.can_read_body else {}
    mode = _validate_mode(hub.cfg, body.get("permissionMode"))
    proc = await hub.ensure_process(sid, model=body.get("model") or None, permission_mode=mode)
    request.app[PROBE].remember(proc.cwd, _probe_info(proc))
    return _json({"state": proc.snapshot()})


async def stop_session(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    # Stopping a session by hand also ends "keep running"; otherwise it would come straight back.
    await hub.auto_resume.taken_over(sid)
    kept = hub.prefs.keep_alive(sid)
    if kept:
        await hub.set_keep_alive(sid, False)
    stopped = await hub.stop_process(sid)
    return _json({"stopped": stopped, "keepAliveCleared": kept})


async def keep_alive(request: web.Request) -> web.Response:
    """Keep a session's claude process running (on: true) or let it stop when idle again."""
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request)
    on = bool(body.get("on"))
    note = await hub.set_keep_alive(sid, on)
    return _json({"keepAlive": on, "note": note, "state": hub.session_state(sid)})


async def interrupt(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    proc = hub.get_hosted(_session_id(request))
    if proc is None:
        return _err(409, "session is not running here")
    body = await _body(request) if request.can_read_body else {}
    result = await proc.interrupt(cancel_queued=bool(body.get("cancelQueued")))
    return _json({"result": result})


async def cancel_message(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request)
    message_uuid = str(body.get("uuid") or "")
    if await request.app[DELIVERY].cancel(sid, message_uuid):
        return _json({"result": {"cancelled": True}})
    proc = hub.get_hosted(sid)
    if proc is None:
        return _err(409, "session is not running here")
    return _json({"result": await proc.cancel_message(message_uuid)})


async def answer_permission(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    proc = hub.get_hosted(_session_id(request))
    if proc is None:
        return _err(409, "session is not running here")
    body = await _body(request)
    request_id = request.match_info["rid"]
    pending = proc.permissions.get(request_id)
    if pending is None:
        return _err(409, "permission request is no longer pending")
    behavior = body.get("behavior")
    if behavior == "allow":
        decision: dict[str, Any] = {"behavior": "allow",
                                    "updatedInput": body.get("updatedInput") or pending.get("input") or {}}
        if isinstance(body.get("updatedPermissions"), list):
            decision["updatedPermissions"] = body["updatedPermissions"]
    elif behavior == "deny":
        decision = {"behavior": "deny", "message": str(body.get("message") or "The user denied this action."),
                    "interrupt": bool(body.get("interrupt"))}
    else:
        return _err(400, "behavior must be allow or deny")
    await proc.answer_permission(request_id, decision)
    return _json({"ok": True})


async def set_model(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    proc = await hub.ensure_process(_session_id(request))
    await proc.set_model(body.get("model") or None)
    return _json({"state": proc.snapshot()})


async def set_api(request: web.Request) -> web.Response:
    """Move a session to another model API (body: api, a route id; model: one it offers, or none for its default)."""
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request)
    model = body.get("model") or None
    if model is not None and not (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/\[\]-]{1,100}", model)):
        return _err(400, "invalid model")
    if not isinstance(body.get("api"), str):
        return _err(400, "api is required")
    if hub.harnesses.has(sid):
        await hub.harnesses.set_api(sid, body["api"], model)
    else:
        await hub.set_api(sid, body["api"], model)
    return _json({"state": hub.session_state(sid)})


async def handoff_session(request: web.Request) -> web.Response:
    """Continue this conversation in another harness (body: harness; api, model, permissionMode for the new session)."""
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request)
    target = body.get("harness")
    if not isinstance(target, str) or target not in ("claude", *HARNESSES):
        return _err(400, "invalid harness")
    api = body.get("api") or None
    if api is not None and not routes.valid(target, api):
        return _err(400, "invalid api")
    model = body.get("model") or None
    if model is not None and not (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/\[\]-]{1,100}", model)):
        return _err(400, "invalid model")
    mode = body.get("permissionMode") or None
    if target == "claude":
        mode = _validate_mode(hub.cfg, mode)
    elif mode is not None and mode not in {m.id for m in HARNESSES[target].driver(cwd="/", sink=None).modes()}:
        return _err(400, "invalid permission mode")
    new_id = await hub.handoff(sid, target, api=api, model=model, mode=mode)
    return _json({"sessionId": new_id})


async def h_fork(request: web.Request, hub: Hub, sid: str) -> web.Response:
    return _json({"sessionId": await hub.harnesses.fork(sid), "uuid": None})


async def h_keep_alive(request: web.Request, hub: Hub, sid: str) -> web.Response:
    on = bool((await _body(request)).get("on"))
    await hub.harnesses.set_keep_alive(sid, on)
    return _json({"keepAlive": on, "note": None, "state": hub.harnesses.state(sid)})


async def h_handoff(request: web.Request, hub: Hub, sid: str) -> web.Response:
    return await handoff_session(request)


async def rename_session(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request)
    title = " ".join(str(body.get("title") or "").split())[:200]
    if not title:
        return _err(400, "title is required")
    await hub.rename(sid, title)
    return _json({"session": hub.session_summary(sid)})


async def set_effort(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    effort = _validate_effort(body.get("effort"))
    if effort is None:
        return _err(400, "effort is required")
    proc = hub.get_hosted(_session_id(request))
    if proc is None:
        return _err(409, "session is not running here")
    await proc.set_effort(effort)
    return _json({"state": proc.snapshot()})


async def set_mode(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    mode = _validate_mode(hub.cfg, body.get("mode"))
    if mode is None:
        return _err(400, "mode is required")
    proc = await hub.ensure_process(_session_id(request))
    await proc.set_permission_mode(mode)
    return _json({"state": proc.snapshot()})


async def fork_session(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request) if request.can_read_body else {}
    mode = _validate_mode(hub.cfg, body.get("permissionMode"))
    proc = await hub.fork_session(_session_id(request), model=body.get("model") or None, permission_mode=mode)
    message_uuid = None
    if body.get("text") or body.get("attachments"):
        message_uuid = await _deliver_new(request, proc, body)
    return _json({"sessionId": proc.session_id, "uuid": message_uuid})


async def takeover(request: web.Request) -> web.Response:
    """Stop an idle Claude Code process running elsewhere, then resume the session here."""
    proc = await request.app[DELIVERY].takeover(_session_id(request))
    return _json({"state": proc.snapshot()})


# ---- history, files, uploads -------------------------------------------------

async def history(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    project = request.query.get("project")
    cwd = request.query.get("cwd")
    if project and not cwd:
        path = hub.projects.path_for(project)
        cwd = str(path) if path else None
    limit = min(int(request.query.get("limit", "100")), 500)
    entries = await asyncio.to_thread(request.app[HISTORY].query, cwd, limit)
    return _json({"history": entries})


def _project_root(request: web.Request) -> Path:
    hub = request.app[HUB]
    pid = request.query.get("project", "")
    shared = hub.shares.project_root(pid)          # files agents sent ("@shared/<session id>", shares.py)
    if shared is not None:
        if not shared.is_dir():
            raise files_mod.FileAccessError("nothing was sent in this session")
        return shared
    path = hub.projects.path_for(pid)
    if path is None or not path.is_dir():
        raise files_mod.FileAccessError("unknown project")
    if not hub.projects.files_allowed(pid):
        raise web.HTTPForbidden(text="file browsing is disabled for this project")
    return path


async def files_list(request: web.Request) -> web.Response:
    root = _project_root(request)
    entries = await asyncio.to_thread(files_mod.list_dir, root, request.query.get("path", ""))
    return _json({"entries": entries})


async def files_content(request: web.Request) -> web.Response:
    root = _project_root(request)
    cfg = request.app[CFG]
    info = await asyncio.to_thread(files_mod.describe_file, root, request.query.get("path", ""), cfg.max_file_bytes)
    return _json({"file": info})


async def files_search(request: web.Request) -> web.Response:
    root = _project_root(request)
    results = await asyncio.to_thread(files_mod.search, root, request.query.get("q", ""))
    return _json({"results": results})


def _hdr_path(path: Path | str) -> str:
    return urllib.parse.quote(str(path), safe="/")


def _from_prefixes(from_dir: str) -> list[str]:
    parts = [p for p in from_dir.strip("/").split("/") if p and p not in (".", "..")]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _view_asset_roots(hub: Hub) -> list[Path]:
    roots = [hub.cfg.workspace_root, Path("/tmp"), hub.cfg.state_dir / "shared", hub.cfg.state_dir / "uploads"]
    for spec in HARNESSES.values():
        for template in spec.driver.artifact_dirs:
            base = template.split("{", 1)[0].rstrip("/")
            if base:
                roots.append(Path(os.path.expanduser(base)))
    return roots


def _file_response(path: Path, headers: dict[str, str]) -> web.FileResponse:
    mime = files_mod.mime_type(path)
    if mime:
        headers["Content-Type"] = mime
    # FileResponse answers Range requests (206), which audio/video seeking and Safari's player rely on.
    return web.FileResponse(path, headers=headers)


async def files_raw(request: web.Request) -> web.StreamResponse:
    hub = request.app[HUB]
    pid = request.query.get("project", "")
    rel = request.query.get("path", "")
    from_dir = request.query.get("from", "")
    ws_root = Path(os.path.realpath(hub.cfg.workspace_root))
    headers = {"Cache-Control": "private, no-cache", "X-Riesz-Workspace-Root": _hdr_path(ws_root)}
    shared_root = hub.shares.project_root(pid)
    if shared_root is not None:
        sid = pid[len(shares_mod.PROJECT_PREFIX):]
        try:
            path, orig_path, key = hub.shares.resolve_raw(sid, rel)
        except files_mod.FileAccessError:
            if not from_dir or "/" not in rel.lstrip("/"):
                raise
            key_part, _, rest = rel.lstrip("/").partition("/")
            path = orig_path = None
            key = key_part
            for prefix in _from_prefixes(from_dir):
                try:
                    path, orig_path, key = hub.shares.resolve_raw(sid, f"{key_part}/{prefix}/{rest}")
                    break
                except files_mod.FileAccessError:
                    continue
            if path is None:
                raise
        proj_root = orig_path.parent if orig_path is not None else shared_root
        headers["X-Riesz-Project-Root"] = _hdr_path(proj_root)
        headers["X-Riesz-File-Path"] = _hdr_path(orig_path or path)
        if key:
            headers["X-Riesz-View-Prefix"] = _hdr_path(key)
        return _file_response(path, headers)
    root = _project_root(request)
    try:
        path = files_mod.raw_path(root, rel)
    except files_mod.FileAccessError:
        if not from_dir:
            raise
        path = None
        for prefix in _from_prefixes(from_dir):
            try:
                path = files_mod.raw_path(root, f"{prefix}/{rel.lstrip('/')}")
                break
            except files_mod.FileAccessError:
                continue
        if path is None:
            raise
    headers["X-Riesz-Project-Root"] = _hdr_path(os.path.realpath(root))
    headers["X-Riesz-File-Path"] = _hdr_path(path)
    return _file_response(path, headers)


async def files_view_asset(request: web.Request) -> web.StreamResponse:
    """Serve an explicit local asset referenced by an HTML preview (called only by backend viewer.py)."""
    hub = request.app[HUB]
    raw = request.query.get("path", "")
    path = files_mod.safe_view_asset(raw, _view_asset_roots(hub), allow_external=hub.cfg.allow_external_files)
    ws_root = Path(os.path.realpath(hub.cfg.workspace_root))
    headers = {
        "Cache-Control": "private, no-cache",
        "X-Riesz-Workspace-Root": _hdr_path(ws_root),
        "X-Riesz-Project-Root": _hdr_path(path.parent),
        "X-Riesz-File-Path": _hdr_path(path),
    }
    return _file_response(path, headers)


async def upload(request: web.Request) -> web.Response:
    store = request.app[UPLOADS]
    reader = await request.multipart()
    field = await reader.next()
    if field is None or getattr(field, "name", None) != "file":
        return _err(400, "missing file field")
    data = bytearray()
    while True:
        chunk = await field.read_chunk(1 << 16)  # type: ignore[union-attr]
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 200 * 1024 * 1024:
            return _err(413, "file too large")
    meta = await asyncio.to_thread(store.save, field.filename or "file", field.headers.get("Content-Type"), bytes(data))  # type: ignore[union-attr]
    return _json({"upload": {k: v for k, v in meta.items() if k != "path"}})


async def upload_raw(request: web.Request) -> web.StreamResponse:
    meta = request.app[UPLOADS].get(request.match_info["uid"])
    if meta is None:
        return _err(404, "not found")
    return web.FileResponse(Path(meta["path"]))


# ---- websocket ---------------------------------------------------------------

async def websocket(request: web.Request) -> web.WebSocketResponse:
    hub = request.app[HUB]
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    subscriptions: dict[str, Any] = {}

    async def sub(event: dict[str, Any]) -> None:
        if queue.qsize() > WS_QUEUE_LIMIT:
            raise RuntimeError("client too slow")
        queue.put_nowait(event)

    async def writer() -> None:
        while True:
            event = await queue.get()
            if event is None:
                return
            await ws.send_str(json.dumps(event, ensure_ascii=False))

    writer_task = asyncio.create_task(writer())
    global_subscribed = False
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            op = data.get("op")
            if op == "sub_global" and not global_subscribed:
                hub.subscribe_global(sub)
                global_subscribed = True
                queue.put_nowait({"ev": "live", "live": hub.live_list()})
                queue.put_nowait(hub.reload_info())
            elif op == "sub":
                sid = str(data.get("sessionId") or "")
                if not _UUID_RE.match(sid):
                    continue
                # A repeated "sub" just asks for a fresh snapshot.
                if hub.harnesses.has(sid):
                    items = hub.harnesses.items(sid)
                    queue.put_nowait({"ev": "snapshot", "sessionId": sid, "items": items[-SNAPSHOT_ITEMS:],
                                      "hasMore": len(items) > SNAPSHOT_ITEMS, "state": hub.harnesses.state(sid),
                                      "session": hub.harnesses.summary(sid)})
                    if sid not in subscriptions:
                        hub.subscribe(sid, sub)
                        subscriptions[sid] = sub
                    continue
                view = await hub.catch_up(sid)
                every = hub.display_items(sid, view.items) if view else []
                items = every[-SNAPSHOT_ITEMS:]
                queue.put_nowait({
                    "ev": "snapshot", "sessionId": sid, "items": items,
                    "hasMore": len(every) > len(items),
                    "state": hub.session_state(sid),
                    "session": hub.session_summary(sid) if hub.store.entries.get(sid) else None,
                })
                if sid not in subscriptions:
                    hub.subscribe(sid, sub)
                    subscriptions[sid] = sub
            elif op == "unsub":
                sid = str(data.get("sessionId") or "")
                if sid in subscriptions:
                    hub.unsubscribe(sid, subscriptions.pop(sid))
            elif op == "ping":
                queue.put_nowait({"ev": "pong", "t": data.get("t")})
    finally:
        if global_subscribed:
            hub.unsubscribe_global(sub)
        for sid, s in subscriptions.items():
            hub.unsubscribe(sid, s)
        queue.put_nowait(None)
        writer_task.cancel()
    return ws



# ---- sessions of other harnesses (harnesses.py) --------------------------------
# Requests for a Codex or DeepSeek Harness session take the same paths as Claude
# Code's; `harness_middleware` hands them to these handlers instead.

HARNESS_EFFORT_RE = re.compile(r"[a-z]{1,16}")


def _harness_mode(hub: Hub, sid: str, mode: Any) -> str | None:
    if mode in (None, ""):
        return None
    if mode not in {m["id"] for m in hub.harnesses.state(sid)["modes"]}:
        raise web.HTTPBadRequest(text="invalid permission mode")
    return str(mode)


def _harness_effort(effort: Any) -> str | None:
    if effort in (None, ""):
        return None
    if not isinstance(effort, str) or not HARNESS_EFFORT_RE.fullmatch(effort):
        raise web.HTTPBadRequest(text="invalid effort level")
    return effort


async def h_detail(request: web.Request, hub: Hub, sid: str) -> web.Response:
    return _json({"session": hub.harnesses.summary(sid), "state": hub.harnesses.state(sid)})


async def h_items(request: web.Request, hub: Hub, sid: str) -> web.Response:
    all_items = hub.harnesses.items(sid)
    before = request.query.get("before")
    limit = min(int(request.query.get("limit", str(PAGE_ITEMS))), 1000)
    items = [it for it in all_items if it["seq"] < int(before)] if before is not None else all_items
    page = items[-limit:]
    return _json({"items": page, "hasMore": len(items) > len(page), "total": len(all_items)})


async def h_image(request: web.Request, hub: Hub, sid: str) -> web.StreamResponse:
    """An image attached here or carried over from an earlier session in its handoff chain."""
    return _serve_image(hub, sid, request.query.get("ref", ""))


async def h_commands(request: web.Request, hub: Hub, sid: str) -> web.Response:
    harness = hub.harnesses.harness_of(sid) or ""
    commands = hub.harnesses.commands(sid)
    found = await _routes_with_models(hub, harness, session_id=sid) if harness in routes.ROUTES else []
    route = hub.harnesses.records[sid].get("api")
    current = next((r for r in found if r["id"] == route), None)
    if current is not None:
        models = current["models"]
    else:
        try:
            models = await hub.harnesses.models(sid)
        except ControlError:
            models = []
    return _json({"commands": commands, "models": models, "source": "session", "defaults": {}, "routes": found})


async def h_context_limits(request: web.Request, hub: Hub, sid: str) -> web.Response:
    context = hub.harnesses.state(sid).get("context") or {}
    if not context.get("window"):
        return _err(503, "context window unknown")
    return _json({"maxTokens": context["window"], "autocompactBuffer": 0, "liveTokens": context.get("used"),
                  "model": hub.harnesses.summary(sid).get("model"), "source": "live"})


async def h_send(request: web.Request, hub: Hub, sid: str) -> web.Response:
    body = await _body(request)
    text, attachments, message_uuid = _prompt(request, body)
    model = body.get("model") or None
    if model is not None and not (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model)):
        return _err(400, "invalid model")
    if request.headers.get("X-Riesz-From") == "web":
        await hub.auto_resume.taken_over(sid)
    result = await request.app[DELIVERY].send(sid, text, attachments, message_uuid, model=model,
                                              permission_mode=_harness_mode(hub, sid, body.get("permissionMode")),
                                              effort=_harness_effort(body.get("effort")))
    return _json({**result, "state": hub.harnesses.state(sid)})


async def h_resume(request: web.Request, hub: Hub, sid: str) -> web.Response:
    if request.headers.get("X-Riesz-From") == "web":
        await hub.auto_resume.taken_over(sid)
    result = await request.app[DELIVERY].send(sid, RESUME_TEXT, [], str(uuidlib.uuid4()))
    return _json({**result, "state": hub.harnesses.state(sid)})


async def h_start(request: web.Request, hub: Hub, sid: str) -> web.Response:
    await hub.harnesses.ensure(sid)
    return _json({"state": hub.harnesses.state(sid)})


async def h_stop(request: web.Request, hub: Hub, sid: str) -> web.Response:
    kept = bool(hub.harnesses.records[sid].get("keepAlive"))
    return _json({"stopped": await hub.harnesses.stop(sid, by_user=True), "keepAliveCleared": kept})


async def h_interrupt(request: web.Request, hub: Hub, sid: str) -> web.Response:
    return _json({"result": await hub.harnesses.interrupt(sid)})


async def h_cancel(request: web.Request, hub: Hub, sid: str) -> web.Response:
    body = await _body(request)
    cancelled = await request.app[DELIVERY].cancel(sid, str(body.get("uuid") or ""))
    return _json({"result": {"cancelled": cancelled}})


async def h_permission(request: web.Request, hub: Hub, sid: str) -> web.Response:
    body = await _body(request)
    if body.get("behavior") not in ("allow", "deny"):
        return _err(400, "behavior must be allow or deny")
    if not hub.harnesses.answer(sid, request.match_info["rid"], body):
        return _err(409, "permission request is no longer pending")
    return _json({"ok": True})


async def h_model(request: web.Request, hub: Hub, sid: str) -> web.Response:
    body = await _body(request)
    model = body.get("model") or None
    if model is not None and not (isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model)):
        return _err(400, "invalid model")
    await hub.harnesses.set(sid, "model", model)
    return _json({"state": hub.harnesses.state(sid)})


async def h_mode(request: web.Request, hub: Hub, sid: str) -> web.Response:
    mode = _harness_mode(hub, sid, (await _body(request)).get("mode"))
    if mode is None:
        return _err(400, "mode is required")
    await hub.harnesses.set(sid, "mode", mode)
    return _json({"state": hub.harnesses.state(sid)})


async def h_effort(request: web.Request, hub: Hub, sid: str) -> web.Response:
    effort = _harness_effort((await _body(request)).get("effort"))
    if effort is None:
        return _err(400, "effort is required")
    await hub.harnesses.set(sid, "effort", effort)
    return _json({"state": hub.harnesses.state(sid)})


async def h_rename(request: web.Request, hub: Hub, sid: str) -> web.Response:
    title = " ".join(str((await _body(request)).get("title") or "").split())[:200]
    if not title:
        return _err(400, "title is required")
    await hub.harnesses.rename(sid, title)
    return _json({"session": hub.harnesses.summary(sid)})


async def h_set_api(request: web.Request, hub: Hub, sid: str) -> web.Response:
    return await set_api(request)


async def h_subagents(request: web.Request, hub: Hub, sid: str) -> web.Response:
    return _json({"subagents": hub.child_entries(sid)})


async def h_subagent_items(request: web.Request, hub: Hub, sid: str) -> web.Response:
    """Display items of a harness's built-in subagent (Antigravity invoke_subagent)."""
    driver = hub.harnesses.drivers.get(sid)
    parent_alive = bool(driver is not None and driver.alive)
    result = await hub.agy_subagents.items(sid, request.match_info["aid"], parent_alive=parent_alive)
    if result is None:
        return _err(404, "unknown subagent")
    items, mtime, running = result
    before = request.query.get("before")
    limit = min(int(request.query.get("limit", str(PAGE_ITEMS))), 1000)
    if before is not None:
        b = int(before)
        items = [it for it in items if it["seq"] < b]
    page = items[-limit:]
    return _json({"items": page, "hasMore": len(items) > len(page), "mtime": mtime, "running": running})


async def h_dismiss(request: web.Request, hub: Hub, sid: str) -> web.Response:
    await hub.clear_interrupted(sid)
    return _json({"ok": True})


async def h_unsupported(request: web.Request, hub: Hub, sid: str) -> web.Response:
    harness = hub.harnesses.harness_of(sid) or "this harness"
    return _err(409, f"not supported for {harness} sessions")


HARNESS_HANDLERS: dict[Any, Any] = {}       # filled in build_app: Claude Code handler -> harness handler


@web.middleware
async def harness_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    sid = request.match_info.get("sid")
    if sid and _UUID_RE.match(sid):
        hub = request.app[HUB]
        if hub.harnesses.has(sid):
            return await HARNESS_HANDLERS.get(request.match_info.handler, h_unsupported)(request, hub, sid)
    return await handler(request)


async def credentials(request: web.Request) -> web.Response:
    """API keys (whether each is set and how, never the key) and the harnesses' subscription logins."""
    hub = request.app[HUB]
    subs = []
    for provider, name in SUBSCRIPTIONS.items():
        login = await hub.credentials.subscription(provider, hub.bins())
        subs.append({"provider": provider, "label": name, "loggedIn": login is not None, "plan": (login or {}).get("plan")})
    return _json({"providers": hub.credentials.status(), "subscriptions": subs})


async def credential_action(request: web.Request) -> web.Response:
    """set (body: key or command), test or clear one provider's API key."""
    hub = request.app[HUB]
    provider = request.match_info["provider"]
    action = request.match_info["action"]
    if provider not in PROVIDERS:
        return _err(404, "unknown provider")
    try:
        if action == "set":
            body = await _body(request)
            if isinstance(body.get("key"), str) and body["key"].strip():
                hub.credentials.set_key(provider, body["key"])
            elif isinstance(body.get("command"), str) and body["command"].strip():
                hub.credentials.set_command(provider, body["command"])
            else:
                return _err(400, "key or command is required")
            result: dict[str, Any] = await hub.credentials.test(provider)
        elif action == "test":
            result = await hub.credentials.test(provider)
        else:
            hub.credentials.clear(provider)
            result = {"ok": True}
    except CredentialError as exc:
        return _err(400, str(exc))
    return _json({**result, "providers": hub.credentials.status()})


async def harnesses(request: web.Request) -> web.Response:
    """The other harnesses this runner can start sessions of, and whether each is ready."""
    return _json({"harnesses": await request.app[HUB].harnesses.available()})


async def harness_models(request: web.Request) -> web.Response:
    harness = request.match_info["harness"]
    if harness not in HARNESSES:
        return _err(404, "unknown harness")
    route = request.query.get("route") or None
    return _json({"models": await request.app[HUB].harnesses.models(harness=harness, route=route)})


# ---- server browser (browser.py) ----------------------------------------------

async def browser_status(request: web.Request) -> web.Response:
    return _json(await request.app[HUB].browser.status())


async def browser_start(request: web.Request) -> web.Response:
    browser = request.app[HUB].browser
    try:
        await browser.ensure_running()
    except RuntimeError as exc:
        return _err(503, str(exc))
    return _json(await browser.status())


async def browser_stop(request: web.Request) -> web.Response:
    browser = request.app[HUB].browser
    await browser.stop()
    return _json(await browser.status())


async def browser_new_tab(request: web.Request) -> web.Response:
    browser = request.app[HUB].browser
    body = await _body(request) if request.can_read_body else {}
    try:
        tab = await browser.new_tab(str(body.get("url") or "about:blank"))
    except RuntimeError as exc:
        return _err(503, str(exc))
    return _json({"tab": tab, "tabs": await browser.list_tabs()})


async def browser_tab_action(request: web.Request) -> web.Response:
    browser = request.app[HUB].browser
    tid = request.match_info["tid"]
    action = request.match_info["action"]
    try:
        if action == "activate":
            await browser.activate_tab(tid)
        elif action == "close":
            await browser.close_tab(tid)
        else:
            return _err(404, "unknown action")
    except RuntimeError as exc:
        return _err(400, str(exc))
    return _json({"tabs": await browser.list_tabs()})


async def browser_ws(request: web.Request) -> web.WebSocketResponse:
    browser = request.app[HUB].browser
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    try:
        await browser.bridge_ws(ws, request.query.get("target") or None)
    except RuntimeError as exc:
        with contextlib.suppress(Exception):
            await ws.send_str(json.dumps({"ev": "error", "message": str(exc)}))
    except Exception:  # noqa: BLE001
        log.exception("browser websocket bridge failed")
    finally:
        with contextlib.suppress(Exception):
            await ws.close()
    return ws


# ---- local computer nodes (nodes.py) -----------------------------------------

async def nodes_list(request: web.Request) -> web.Response:
    sid = request.query.get("session") or None
    return _json(request.app[HUB].nodes.snapshot(sid))


async def nodes_pair(request: web.Request) -> web.Response:
    body = await _body(request) if request.can_read_body else {}
    sid = str(body.get("sessionId") or "")
    if not _UUID_RE.match(sid):
        return _err(400, "invalid sessionId")
    try:
        pair = request.app[HUB].nodes.create_pairing(
            session_id=sid,
            mode=str(body.get("mode") or "auto"),
            ttl=int(body.get("ttl") or 300),
        )
    except NodeError as exc:
        return _err(400, str(exc))
    return _json({"pairing": pair, **request.app[HUB].nodes.snapshot()})


async def nodes_register(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        res = request.app[HUB].nodes.register_node(
            code=str(body.get("code") or ""),
            public_key=str(body.get("publicKey") or ""),
            name=str(body.get("name") or ""),
            os_name=str(body.get("os") or ""),
            platform=str(body.get("platform") or ""),
            hostname=str(body.get("hostname") or ""),
        )
    except NodeError as exc:
        return _err(400, str(exc))
    return _json(res)


async def nodes_node_action(request: web.Request) -> web.Response:
    nodes = request.app[HUB].nodes
    nid = request.match_info["nid"]
    action = request.match_info["action"]
    body = await _body(request) if request.can_read_body else {}
    try:
        if action == "kick":
            await nodes.kick_node(nid)
        elif action == "rename":
            await nodes.rename_node(nid, str(body.get("name") or ""))
        elif action == "mode":
            await nodes.set_mode(nid, str(body.get("mode") or "auto"))
        else:
            return _err(404, "unknown action")
    except NodeError as exc:
        return _err(400, str(exc))
    return _json(nodes.snapshot())


async def nodes_approval_resolve(request: web.Request) -> web.Response:
    nodes = request.app[HUB].nodes
    aid = request.match_info["aid"]
    body = await _body(request)
    ok = await nodes.resolve_approval(aid, allow=bool(body.get("allow")), decided_by="web")
    if not ok:
        return _err(404, "approval not found or already resolved")
    return _json(nodes.snapshot())


async def nodes_command_kill(request: web.Request) -> web.Response:
    nodes = request.app[HUB].nodes
    nid = request.match_info["nid"]
    cid = request.match_info["cid"]
    ok = await nodes.kill_command(nid, cid)
    if not ok:
        return _err(404, "command not running")
    return _json({"killed": True})


async def nodes_transfer_get(request: web.Request) -> web.StreamResponse:
    nodes = request.app[HUB].nodes
    token = request.match_info["token"]
    try:
        ticket = nodes.claim_transfer(token, "push")
    except NodeError as exc:
        return _err(404, str(exc))
    if not ticket.server_path.is_file():
        return _err(404, "source file missing")
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Riesz-SHA256": ticket.sha256 or "",
        "Cache-Control": "no-store",
    }
    return web.FileResponse(ticket.server_path, headers=headers)


async def nodes_transfer_post(request: web.Request) -> web.Response:
    import hashlib
    nodes = request.app[HUB].nodes
    token = request.match_info["token"]
    try:
        ticket = nodes.claim_transfer(token, "pull")
    except NodeError as exc:
        return _err(404, str(exc))
    expected_sha = request.headers.get("X-Riesz-SHA256", "").strip().lower()
    dest = ticket.server_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{uuidlib.uuid4().hex[:8]}.tmp")
    h = hashlib.sha256()
    total = 0
    try:
        with tmp.open("wb") as fh:
            async for chunk in request.content.iter_chunked(256 * 1024):
                total += len(chunk)
                if total > MAX_TRANSFER_BYTES:
                    raise NodeError("文件超过 200MB 限制")
                h.update(chunk)
                fh.write(chunk)
        digest = h.hexdigest()
        if expected_sha and digest != expected_sha:
            raise NodeError("文件 SHA-256 校验失败")
        os.replace(tmp, dest)
        ticket.size = total
        ticket.sha256 = digest
    except Exception as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return _err(400, str(exc))
    return _json({"ok": True, "size": total, "sha256": ticket.sha256})


async def nodes_client_zip(request: web.Request) -> web.Response:
    try:
        raw, digest = await asyncio.to_thread(build_client_zip)
    except NodeError as exc:
        return _err(404, str(exc))
    return web.Response(
        body=raw,
        content_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="riesz-node.zip"',
            "X-Riesz-Client-Hash": digest,
            "Cache-Control": "no-cache",
        },
    )


async def nodes_bootstrap_py(request: web.Request) -> web.Response:
    origin = request.query.get("origin") or "https://your-server"
    token = request.query.get("token") or ""
    try:
        script = await asyncio.to_thread(render_bootstrap_py, origin, token=token)
    except NodeError as exc:
        return _err(404, str(exc))
    return web.Response(text=script, content_type="text/x-python", headers={"Cache-Control": "no-store"})


async def nodes_ws(request: web.Request) -> web.WebSocketResponse:
    nodes = request.app[HUB].nodes
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    try:
        await nodes.handle_ws(ws)
    except Exception:  # noqa: BLE001
        log.exception("local node websocket error")
    finally:
        with contextlib.suppress(Exception):
            await ws.close()
    return ws


# ---- app ---------------------------------------------------------------------

def build_app(cfg: Config) -> web.Application:
    app = web.Application(middlewares=[auth_middleware, harness_middleware], client_max_size=210 * 1024 * 1024)
    hub = Hub(cfg)
    app[CFG] = cfg
    app[HUB] = hub
    app[UPLOADS] = UploadStore(cfg.uploads_dir)
    app[HISTORY] = PromptHistory(cfg.history_file, cfg.own_history_file)
    app[PROBE] = CapabilityProbe(cfg)
    app[DELIVERY] = Delivery(hub, app[HISTORY])
    app[LIMITS] = ContextLimits(cfg)
    hub.delivery = app[DELIVERY]

    async def on_startup(app: web.Application) -> None:
        await hub.start()
        await asyncio.to_thread(app[UPLOADS].cleanup)

    async def on_cleanup(app: web.Application) -> None:
        await hub.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    r = app.router
    r.add_get("/v1/health", health)
    r.add_get("/v1/usage", plan_usage)
    r.add_get("/v1/balances", balances)
    r.add_get("/v1/limits", subscription_limits)
    r.add_get("/v1/projects", list_projects)
    r.add_post("/v1/projects", create_project)
    r.add_get("/v1/projects/{pid}", project_detail)
    r.add_get("/v1/sessions", list_sessions)
    r.add_post("/v1/sessions", create_session)
    r.add_get("/v1/live", live_sessions)
    r.add_get("/v1/commands", session_commands)
    r.add_get("/v1/sessions/{sid}", session_detail)
    r.add_get("/v1/sessions/{sid}/items", session_items)
    r.add_get("/v1/sessions/{sid}/subagents", session_subagents)
    r.add_get("/v1/sessions/{sid}/subagents/{aid}/items", subagent_items)
    r.add_post("/v1/sessions/{sid}/tasks/{tid}/stop", stop_task)
    r.add_post("/v1/sessions/{sid}/background", background_task)
    r.add_get("/v1/sessions/{sid}/image", session_image)
    r.add_get("/v1/sessions/{sid}/commands", session_commands)
    r.add_get("/v1/sessions/{sid}/context", context_usage)
    r.add_get("/v1/sessions/{sid}/context-limits", context_limits)
    r.add_post("/v1/sessions/{sid}/messages", send_message)
    r.add_post("/v1/sessions/{sid}/start", start_session)
    r.add_post("/v1/sessions/{sid}/stop", stop_session)
    r.add_post("/v1/sessions/{sid}/keep-alive", keep_alive)
    r.add_post("/v1/sessions/{sid}/resume", resume_session)
    r.add_get("/v1/sessions/{sid}/shared", session_shared)
    r.add_post("/v1/sessions/{sid}/open-file", open_file)
    r.add_get("/v1/tools", list_tools)
    r.add_post("/v1/tools/call", call_tool)
    r.add_get("/v1/schedules", list_schedules)
    r.add_post("/v1/schedules", create_schedule)
    r.add_post(r"/v1/schedules/{id:[0-9a-f]{1,32}}/{action:update|run|delete}", schedule_action)
    r.add_get("/v1/notes", list_notes)
    r.add_post("/v1/notes", create_note)
    r.add_post("/v1/notes/clear", clear_notes)
    r.add_post(r"/v1/notes/{id:[0-9a-f]{1,32}}/{action:update|delete}", note_action)
    r.add_post("/v1/sessions/{sid}/dismiss-interrupted", dismiss_interrupted)
    r.add_post("/v1/sessions/{sid}/cancel-auto-resume", cancel_auto_resume)
    r.add_post("/v1/sessions/{sid}/interrupt", interrupt)
    r.add_post("/v1/sessions/{sid}/cancel", cancel_message)
    r.add_post("/v1/sessions/{sid}/permissions/{rid}", answer_permission)
    r.add_post("/v1/sessions/{sid}/model", set_model)
    r.add_post("/v1/sessions/{sid}/mode", set_mode)
    r.add_post("/v1/sessions/{sid}/effort", set_effort)
    r.add_post("/v1/sessions/{sid}/rename", rename_session)
    r.add_post("/v1/sessions/{sid}/fork", fork_session)
    r.add_post("/v1/sessions/{sid}/takeover", takeover)
    r.add_get("/v1/history", history)
    r.add_get("/v1/files/list", files_list)
    r.add_get("/v1/files/content", files_content)
    r.add_get("/v1/files/raw", files_raw)
    r.add_get("/v1/files/view-asset", files_view_asset)
    r.add_get("/v1/files/search", files_search)
    r.add_post("/v1/uploads", upload)
    r.add_get("/v1/uploads/{uid}", upload_raw)
    r.add_get("/v1/ws", websocket)
    r.add_get("/v1/browser/status", browser_status)
    r.add_post("/v1/browser/start", browser_start)
    r.add_post("/v1/browser/stop", browser_stop)
    r.add_post("/v1/browser/tabs", browser_new_tab)
    r.add_post(r"/v1/browser/tabs/{tid:[A-Za-z0-9_-]{1,128}}/{action:activate|close}", browser_tab_action)
    r.add_get("/v1/browser/ws", browser_ws)
    r.add_get("/v1/nodes", nodes_list)
    r.add_post("/v1/nodes/pair", nodes_pair)
    r.add_post("/v1/nodes/register", nodes_register)
    r.add_post(r"/v1/nodes/{nid:[0-9a-f]{1,32}}/{action:kick|rename|mode}", nodes_node_action)
    r.add_post(r"/v1/nodes/{nid:[0-9a-f]{1,32}}/commands/{cid:[A-Za-z0-9_-]{1,32}}/kill", nodes_command_kill)
    r.add_post(r"/v1/nodes/approvals/{aid:[A-Za-z0-9_-]{1,32}}/resolve", nodes_approval_resolve)
    r.add_get(r"/v1/nodes/transfer/{token:[A-Za-z0-9_-]{16,128}}", nodes_transfer_get)
    r.add_post(r"/v1/nodes/transfer/{token:[A-Za-z0-9_-]{16,128}}", nodes_transfer_post)
    r.add_get("/v1/nodes/client.zip", nodes_client_zip)
    r.add_get("/v1/nodes/bootstrap.py", nodes_bootstrap_py)
    r.add_get("/v1/nodes/ws", nodes_ws)
    r.add_get("/v1/harnesses", harnesses)
    r.add_get("/v1/credentials", credentials)
    r.add_post(r"/v1/credentials/{provider:[a-z]{1,16}}/{action:set|test|clear}", credential_action)
    r.add_post("/v1/sessions/{sid}/api", set_api)
    r.add_post("/v1/sessions/{sid}/handoff", handoff_session)
    r.add_get(r"/v1/harnesses/{harness:[a-z]{1,16}}/models", harness_models)
    HARNESS_HANDLERS.update({
        session_shared: lambda request, _hub, _sid: session_shared(request),
        open_file: lambda request, _hub, _sid: open_file(request),
        cancel_auto_resume: lambda request, _hub, _sid: cancel_auto_resume(request),
        session_detail: h_detail, session_items: h_items, session_image: h_image, session_commands: h_commands,
        context_limits: h_context_limits, send_message: h_send, resume_session: h_resume, start_session: h_start,
        stop_session: h_stop, interrupt: h_interrupt, cancel_message: h_cancel, answer_permission: h_permission,
        set_model: h_model, set_mode: h_mode, set_effort: h_effort, rename_session: h_rename, set_api: h_set_api, handoff_session: h_handoff,
        fork_session: h_fork, keep_alive: h_keep_alive,
        session_subagents: h_subagents, subagent_items: h_subagent_items, dismiss_interrupted: h_dismiss,
    })
    return app


__all__ = ["build_app", "ROOT_ID"]
