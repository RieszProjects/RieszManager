"""HTTP + WebSocket API of the runner (served on a Unix domain socket)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import files as files_mod
from .claude_settings import user_defaults
from .config import Config
from .context_limits import ContextLimits, limits_from_usage
from .delivery import Delivery
from .history import PromptHistory
from .hub import Hub
from .probe import CapabilityProbe
from .process import ControlError
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
        return _err(401, "unauthorized")
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


async def session_items(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    view = await hub.catch_up(sid)
    if view is None:
        return _json({"items": [], "hasMore": False, "total": 0})
    before = request.query.get("before")
    limit = min(int(request.query.get("limit", str(PAGE_ITEMS))), 1000)
    items = view.items
    if before is not None:
        b = int(before)
        items = [it for it in items if it["seq"] < b]
    page = items[-limit:]
    return _json({"items": page, "hasMore": len(items) > len(page), "total": len(view.items)})


async def session_image(request: web.Request) -> web.StreamResponse:
    hub = request.app[HUB]
    sid = _session_id(request)
    ref = request.query.get("ref", "")
    offset_s, _, path = ref.partition(":")
    if not offset_s.isdigit() or not path:
        return _err(400, "invalid ref")
    line = hub.store.read_line_at(sid, int(offset_s))
    if not line:
        return _err(404, "not found")
    resolved = resolve_image(line, path)
    if resolved is None:
        return _err(404, "not found")
    data, mime = resolved
    return web.Response(body=data, content_type=mime, headers={"Cache-Control": "private, max-age=86400"})


async def session_commands(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    probe = request.app[PROBE]
    sid = request.match_info.get("sid")
    proc = hub.get_hosted(sid) if sid else None
    if proc is not None and proc.init_info:
        models = proc.models()
        return _json({"commands": proc.commands(), "models": models, "source": "session",
                      "defaults": user_defaults(hub.cfg.claude_home, models)})
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
    return _json({"commands": info["commands"], "models": info["models"], "source": "probe",
                  "defaults": user_defaults(hub.cfg.claude_home, info["models"])})


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
    mode = _validate_mode(hub.cfg, body.get("permissionMode"))
    proc = await hub.create_session(str(path), model=body.get("model") or None, permission_mode=mode,
                                    effort=_validate_effort(body.get("effort")))
    request.app[PROBE].remember(proc.cwd, _probe_info(proc))
    if started_by:
        hub.prefs.remember(proc.session_id, startedBy=started_by)
    message_uuid = None
    if body.get("text") or body.get("attachments"):
        message_uuid = await _deliver_new(request, proc, body)
    if body.get("keepAlive"):
        await hub.set_keep_alive(proc.session_id, True)
    return _json({"sessionId": proc.session_id, "uuid": message_uuid, "state": proc.snapshot()})


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
    result = await request.app[DELIVERY].send(
        sid, text, attachments, message_uuid,
        model=body.get("model") or None, permission_mode=mode, effort=_validate_effort(body.get("effort")),
    )
    return _json({**result, "state": hub.session_state(sid)})


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


async def files_raw(request: web.Request) -> web.StreamResponse:
    root = _project_root(request)
    path = files_mod.raw_path(root, request.query.get("path", ""))
    headers = {"Cache-Control": "private, no-cache"}
    audio = files_mod.audio_type(path)
    if audio:
        headers["Content-Type"] = audio
    # FileResponse answers Range requests (206), which audio seeking and Safari's player rely on.
    return web.FileResponse(path, headers=headers)


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
            elif op == "sub":
                sid = str(data.get("sessionId") or "")
                if not _UUID_RE.match(sid):
                    continue
                # A repeated "sub" just asks for a fresh snapshot.
                view = await hub.catch_up(sid)
                items = view.items[-SNAPSHOT_ITEMS:] if view else []
                queue.put_nowait({
                    "ev": "snapshot", "sessionId": sid, "items": items,
                    "hasMore": bool(view and len(view.items) > len(items)),
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


# ---- app ---------------------------------------------------------------------

def build_app(cfg: Config) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=210 * 1024 * 1024)
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
    r.add_get("/v1/projects", list_projects)
    r.add_post("/v1/projects", create_project)
    r.add_get("/v1/projects/{pid}", project_detail)
    r.add_get("/v1/sessions", list_sessions)
    r.add_post("/v1/sessions", create_session)
    r.add_get("/v1/live", live_sessions)
    r.add_get("/v1/commands", session_commands)
    r.add_get("/v1/sessions/{sid}", session_detail)
    r.add_get("/v1/sessions/{sid}/items", session_items)
    r.add_get("/v1/sessions/{sid}/image", session_image)
    r.add_get("/v1/sessions/{sid}/commands", session_commands)
    r.add_get("/v1/sessions/{sid}/context", context_usage)
    r.add_get("/v1/sessions/{sid}/context-limits", context_limits)
    r.add_post("/v1/sessions/{sid}/messages", send_message)
    r.add_post("/v1/sessions/{sid}/start", start_session)
    r.add_post("/v1/sessions/{sid}/stop", stop_session)
    r.add_post("/v1/sessions/{sid}/keep-alive", keep_alive)
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
    r.add_get("/v1/files/search", files_search)
    r.add_post("/v1/uploads", upload)
    r.add_get("/v1/uploads/{uid}", upload_raw)
    r.add_get("/v1/ws", websocket)
    return app


__all__ = ["build_app", "ROOT_ID"]
