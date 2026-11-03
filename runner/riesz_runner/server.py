"""HTTP + WebSocket API of the runner (served on a Unix domain socket)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import signal
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import files as files_mod
from .config import Config
from .history import PromptHistory
from .hub import Hub
from .probe import CapabilityProbe
from .process import ControlError
from .projects import ROOT_ID
from .transcript import resolve_image
from .uploads import UploadStore, compose_content

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
                  "allowBypass": cfg.allow_bypass, "time": time.time()})


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
        return _json({"commands": proc.commands(), "models": proc.models(), "source": "session"})
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
    return _json({"commands": info["commands"], "models": info["models"], "source": "probe"})


async def context_usage(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    proc = hub.get_hosted(_session_id(request))
    if proc is None:
        return _err(409, "session is not running here")
    return _json({"context": await proc.control("get_context_usage")})


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


async def _deliver(request: web.Request, proc: Any, body: dict[str, Any]) -> str:
    text = str(body.get("text") or "")
    attachments = _attachments(request, body.get("attachments"))
    if not text.strip() and not attachments:
        raise web.HTTPBadRequest(text="empty message")
    content, preview = await asyncio.to_thread(compose_content, text, attachments)
    message_uuid = body.get("uuid") if _UUID_RE.match(str(body.get("uuid") or "")) else str(uuidlib.uuid4())
    await proc.send_user(content, message_uuid, preview)
    request.app[HISTORY].record(text, proc.cwd, proc.session_id)
    return message_uuid


async def create_session(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    body = await _body(request)
    project = body.get("project")
    path = hub.projects.path_for(project) if project else None
    if path is None:
        return _err(400, "unknown project")
    mode = _validate_mode(hub.cfg, body.get("permissionMode"))
    proc = await hub.create_session(str(path), model=body.get("model") or None, permission_mode=mode,
                                    effort=body.get("effort") or None)
    request.app[PROBE].remember(proc.cwd, _probe_info(proc))
    message_uuid = None
    if body.get("text") or body.get("attachments"):
        message_uuid = await _deliver(request, proc, body)
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
    proc = await hub.ensure_process(sid, model=body.get("model") or None, permission_mode=mode)
    message_uuid = await _deliver(request, proc, body)
    return _json({"uuid": message_uuid, "state": proc.snapshot()})


async def start_session(request: web.Request) -> web.Response:
    hub = request.app[HUB]
    sid = _session_id(request)
    body = await _body(request) if request.can_read_body else {}
    mode = _validate_mode(hub.cfg, body.get("permissionMode"))
    proc = await hub.ensure_process(sid, model=body.get("model") or None, permission_mode=mode)
    request.app[PROBE].remember(proc.cwd, _probe_info(proc))
    return _json({"state": proc.snapshot()})


async def stop_session(request: web.Request) -> web.Response:
    stopped = await request.app[HUB].stop_process(_session_id(request))
    return _json({"stopped": stopped})


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
    proc = hub.get_hosted(_session_id(request))
    if proc is None:
        return _err(409, "session is not running here")
    body = await _body(request)
    return _json({"result": await proc.cancel_message(str(body.get("uuid") or ""))})


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
        message_uuid = await _deliver(request, proc, body)
    return _json({"sessionId": proc.session_id, "uuid": message_uuid})


async def takeover(request: web.Request) -> web.Response:
    """Stop an idle Claude Code process running elsewhere, then resume the session here."""
    hub = request.app[HUB]
    sid = _session_id(request)
    live = hub.live.get(sid)
    if live is None:
        proc = await hub.ensure_process(sid)
        return _json({"state": proc.snapshot()})
    if live.status == "busy":
        return _err(409, "the session is busy in another process; wait until it is idle")
    try:
        os.kill(live.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        return _err(403, "not allowed to stop that process")
    for _ in range(50):
        await asyncio.sleep(0.2)
        try:
            os.kill(live.pid, 0)
        except ProcessLookupError:
            break
    else:
        return _err(409, "the other process did not exit")
    hub.live.pop(sid, None)
    proc = await hub.ensure_process(sid)
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


async def files_raw(request: web.Request) -> web.StreamResponse:
    root = _project_root(request)
    path = files_mod.raw_path(root, request.query.get("path", ""))
    return web.FileResponse(path, headers={"Cache-Control": "private, no-cache"})


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
                if not _UUID_RE.match(sid) or sid in subscriptions:
                    continue
                view = await hub.catch_up(sid)
                items = view.items[-SNAPSHOT_ITEMS:] if view else []
                queue.put_nowait({
                    "ev": "snapshot", "sessionId": sid, "items": items,
                    "hasMore": bool(view and len(view.items) > len(items)),
                    "state": hub.session_state(sid),
                    "session": hub.session_summary(sid) if hub.store.entries.get(sid) else None,
                })
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

    async def on_startup(app: web.Application) -> None:
        await hub.start()
        await asyncio.to_thread(app[UPLOADS].cleanup)

    async def on_cleanup(app: web.Application) -> None:
        await hub.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    r = app.router
    r.add_get("/v1/health", health)
    r.add_get("/v1/projects", list_projects)
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
    r.add_post("/v1/sessions/{sid}/messages", send_message)
    r.add_post("/v1/sessions/{sid}/start", start_session)
    r.add_post("/v1/sessions/{sid}/stop", stop_session)
    r.add_post("/v1/sessions/{sid}/interrupt", interrupt)
    r.add_post("/v1/sessions/{sid}/cancel", cancel_message)
    r.add_post("/v1/sessions/{sid}/permissions/{rid}", answer_permission)
    r.add_post("/v1/sessions/{sid}/model", set_model)
    r.add_post("/v1/sessions/{sid}/mode", set_mode)
    r.add_post("/v1/sessions/{sid}/fork", fork_session)
    r.add_post("/v1/sessions/{sid}/takeover", takeover)
    r.add_get("/v1/history", history)
    r.add_get("/v1/files/list", files_list)
    r.add_get("/v1/files/content", files_content)
    r.add_get("/v1/files/raw", files_raw)
    r.add_post("/v1/uploads", upload)
    r.add_get("/v1/uploads/{uid}", upload_raw)
    r.add_get("/v1/ws", websocket)
    return app


__all__ = ["build_app", "ROOT_ID"]
