"""Authenticated HTTP endpoints that forward to the host runner."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
from typing import Any
from urllib.parse import quote

import aiohttp
from django.conf import settings
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from riesz.http import ApiError, api_view, error_response, ok

from . import images, runner

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# (method, public path regex, runner path template). Public paths are relative to /api/.
ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    (m, re.compile(p), t)
    for m, p, t in [
        ("GET", r"^projects$", "/v1/projects"),
        ("POST", r"^projects$", "/v1/projects"),
        ("GET", r"^projects/(?P<pid>[^/]{1,512})$", "/v1/projects/{pid}"),
        ("GET", r"^sessions$", "/v1/sessions"),
        ("POST", r"^sessions$", "/v1/sessions"),
        ("GET", r"^live$", "/v1/live"),
        ("GET", r"^usage$", "/v1/usage"),
        ("GET", r"^balances$", "/v1/balances"),
        ("GET", r"^limits$", "/v1/limits"),
        ("GET", r"^schedules$", "/v1/schedules"),
        ("POST", r"^schedules$", "/v1/schedules"),
        ("POST", r"^schedules/(?P<id>[0-9a-f]{1,32})/(?P<action>update|run|delete)$", "/v1/schedules/{id}/{action}"),
        ("GET", r"^notes$", "/v1/notes"),
        ("POST", r"^notes$", "/v1/notes"),
        ("POST", r"^notes/clear$", "/v1/notes/clear"),
        ("POST", r"^notes/(?P<id>[0-9a-f]{1,32})/(?P<action>update|delete)$", "/v1/notes/{id}/{action}"),
        ("GET", r"^commands$", "/v1/commands"),
        ("GET", r"^harnesses$", "/v1/harnesses"),
        ("GET", r"^credentials$", "/v1/credentials"),
        ("POST", r"^credentials/(?P<provider>[a-z]{1,16})/(?P<action>set|test|clear)$", "/v1/credentials/{provider}/{action}"),
        ("GET", r"^harnesses/(?P<harness>[a-z]{1,16})/models$", "/v1/harnesses/{harness}/models"),
        ("GET", rf"^sessions/(?P<sid>{UUID})$", "/v1/sessions/{sid}"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/items$", "/v1/sessions/{sid}/items"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/subagents$", "/v1/sessions/{sid}/subagents"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/subagents/(?P<aid>[A-Za-z0-9_-]{{1,64}})/items$",
         "/v1/sessions/{sid}/subagents/{aid}/items"),
        ("POST", rf"^sessions/(?P<sid>{UUID})/tasks/(?P<tid>[A-Za-z0-9_-]{{1,100}})/stop$", "/v1/sessions/{sid}/tasks/{tid}/stop"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/commands$", "/v1/sessions/{sid}/commands"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/context$", "/v1/sessions/{sid}/context"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/context-limits$", "/v1/sessions/{sid}/context-limits"),
        ("POST", rf"^sessions/(?P<sid>{UUID})/(?P<action>messages|start|stop|interrupt|cancel|model|mode|effort|api|handoff|rename|fork|takeover|keep-alive|background|resume|dismiss-interrupted|cancel-auto-resume)$",
         "/v1/sessions/{sid}/{action}"),
        ("POST", rf"^sessions/(?P<sid>{UUID})/permissions/(?P<rid>[\w-]{{1,80}})$",
         "/v1/sessions/{sid}/permissions/{rid}"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/shared$", "/v1/sessions/{sid}/shared"),
        ("POST", rf"^sessions/(?P<sid>{UUID})/open-file$", "/v1/sessions/{sid}/open-file"),
        ("GET", r"^history$", "/v1/history"),
        ("GET", r"^files/(?P<op>list|content|search)$", "/v1/files/{op}"),
        ("GET", r"^browser/status$", "/v1/browser/status"),
        ("POST", r"^browser/(?P<action>start|stop|tabs)$", "/v1/browser/{action}"),
        ("POST", r"^browser/tabs/(?P<tid>[A-Za-z0-9_-]{1,128})/(?P<action>activate|close)$",
         "/v1/browser/tabs/{tid}/{action}"),
        ("GET", r"^nodes$", "/v1/nodes"),
        ("POST", r"^nodes/pair$", "/v1/nodes/pair"),
        ("POST", r"^nodes/(?P<nid>[0-9a-f]{1,32})/(?P<action>kick|rename|mode)$", "/v1/nodes/{nid}/{action}"),
        ("POST", r"^nodes/(?P<nid>[0-9a-f]{1,32})/commands/(?P<cid>[A-Za-z0-9_-]{1,32})/kill$",
         "/v1/nodes/{nid}/commands/{cid}/kill"),
        ("POST", r"^nodes/approvals/(?P<aid>[A-Za-z0-9_-]{1,32})/resolve$", "/v1/nodes/approvals/{aid}/resolve"),
    ]
]

STREAM_ROUTES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"^sessions/(?P<sid>{UUID})/image$"), "/v1/sessions/{sid}/image"),
    (re.compile(r"^files/raw$"), "/v1/files/raw"),
    (re.compile(r"^uploads/(?P<uid>[0-9a-f]{32})$"), "/v1/uploads/{uid}"),
]


def _match(method: str, path: str) -> str | None:
    for m, pattern, template in ROUTES:
        if m != method:
            continue
        found = pattern.match(path)
        if found:
            return template.format(**found.groupdict())
    return None


def _unavailable() -> HttpResponse:
    return error_response(503, "宿主机运行器未连接", "runner_unavailable")


@require_http_methods(["GET", "POST"])
@api_view()
async def proxy(request: HttpRequest, path: str) -> HttpResponse:
    target = _match(request.method or "GET", path)
    if target is None:
        return error_response(404, "not found")
    body: Any = None
    if request.method == "POST":
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            raise ApiError(400, "请求格式错误")
    try:
        resp = await runner.request(request.method or "GET", target, params=list(request.GET.items()), json=body,
                                    headers={"X-Riesz-From": "web"})
    except runner.RunnerUnavailable:
        return _unavailable()
    async with resp:
        payload = await resp.read()
        return HttpResponse(payload, status=resp.status, content_type="application/json")


@require_http_methods(["GET"])
@api_view()
async def stream(request: HttpRequest, path: str) -> HttpResponse:
    for pattern, template in STREAM_ROUTES:
        found = pattern.match(path)
        if found:
            target = template.format(**found.groupdict())
            break
    else:
        return error_response(404, "not found")
    # Pass byte ranges through: audio seeking needs them, and Safari will not play media without them.
    forward = {h: request.headers[h] for h in ("Range", "If-Range") if h in request.headers}
    try:
        resp = await runner.request("GET", target, params=list(request.GET.items()), headers=forward or None, timeout=600)
    except runner.RunnerUnavailable:
        return _unavailable()
    if resp.status == 416:
        async with resp:
            out = HttpResponse(b"", status=416)
            if resp.headers.get("Content-Range"):
                out["Content-Range"] = resp.headers["Content-Range"]
            return out
    if resp.status not in (200, 206):
        async with resp:
            return HttpResponse(await resp.read(), status=resp.status, content_type="application/json")

    async def body():
        try:
            async for chunk in resp.content.iter_chunked(1 << 16):
                yield chunk
        finally:
            resp.release()

    out = StreamingHttpResponse(body(), status=resp.status,
                                content_type=resp.headers.get("Content-Type", "application/octet-stream"))
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
        if resp.headers.get(name):
            out[name] = resp.headers[name]
    out["Cache-Control"] = resp.headers.get("Cache-Control", "private, no-cache")
    out["X-Content-Type-Options"] = "nosniff"
    if request.GET.get("download"):
        name = request.GET.get("name") or path.rsplit("/", 1)[-1]
        out["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    elif not out["Content-Type"].startswith("application/pdf"):
        # Never let uploaded/project files execute as HTML in our origin.
        out["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"
    return out


@require_http_methods(["POST"])
@api_view()
async def upload(request: HttpRequest) -> HttpResponse:
    file = request.FILES.get("file")
    if file is None:
        raise ApiError(400, "缺少文件")
    if file.size > settings.MAX_UPLOAD_BYTES:
        raise ApiError(413, "文件过大")
    data, name, mime = file.read(), file.name, file.content_type or "application/octet-stream"
    if images.wants_conversion(name, mime):
        converted = await asyncio.to_thread(images.to_jpeg, data, name)
        if converted is not None:
            data, name = converted
            mime = "image/jpeg"
    form = aiohttp.FormData()
    form.add_field("file", data, filename=name, content_type=mime)
    try:
        resp = await runner.request("POST", "/v1/uploads", data=form, timeout=300)
    except runner.RunnerUnavailable:
        return _unavailable()
    async with resp:
        return HttpResponse(await resp.read(), status=resp.status, content_type="application/json")


@require_http_methods(["GET"])
@api_view()
async def app_config(request: HttpRequest) -> HttpResponse:
    health: dict[str, Any] = {}
    try:
        resp = await runner.request("GET", "/v1/health", timeout=5)
        async with resp:
            if resp.status == 200:
                health = await resp.json()
    except runner.RunnerUnavailable:
        pass
    return ok({
        "runner": bool(health.get("ok")),
        "workspaceRoot": health.get("workspaceRoot"),
        "allowBypass": bool(health.get("allowBypass")),
        "defaultPermissionMode": health.get("defaultPermissionMode") or "default",
        "transcription": bool(settings.OPENAI_API_KEY),
        "transcriptionLive": bool(settings.OPENAI_API_KEY and settings.TRANSCRIBE_LIVE_MODEL),
        "username": request.user.username,
    })


@csrf_exempt
@require_http_methods(["POST"])
async def node_register(request: HttpRequest) -> HttpResponse:
    """One-time pairing endpoint for `riesz-node` clients (authenticated by pairing code, not cookies)."""
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return error_response(400, "invalid json")
    try:
        resp = await runner.request("POST", "/v1/nodes/register", json=body, timeout=30)
    except runner.RunnerUnavailable:
        return _unavailable()
    async with resp:
        return HttpResponse(await resp.read(), status=resp.status, content_type="application/json")


_cached_download_token: str | None = None


def _get_node_download_token() -> tuple[str, bool]:
    """Return `(token, pinned_in_env)` for gating `/api/nodes/bootstrap.py` and `/api/nodes/client.zip`."""
    global _cached_download_token
    env_tok = (getattr(settings, "NODE_DOWNLOAD_TOKEN", "") or "").strip()
    if env_tok:
        return env_tok, True
    if _cached_download_token:
        return _cached_download_token, False
    path = settings.DATA_DIR / "node_download_token"
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            _cached_download_token = raw
            return raw, False
    tok = secrets.token_urlsafe(24)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tok + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _cached_download_token = tok
    return tok, False


def _rotate_node_download_token() -> str:
    global _cached_download_token
    if (getattr(settings, "NODE_DOWNLOAD_TOKEN", "") or "").strip():
        raise ApiError(400, "下载口令已在 .env (RIESZ_NODE_DOWNLOAD_TOKEN) 中固定，请直接修改 .env")
    path = settings.DATA_DIR / "node_download_token"
    tok = secrets.token_urlsafe(24)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tok + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _cached_download_token = tok
    return tok


def _check_download_token(request: HttpRequest) -> str | None:
    """Validate secret download token from `?token=` or `X-Riesz-Download-Token`. Returns token if valid."""
    expected, _ = _get_node_download_token()
    provided = (request.GET.get("token") or request.headers.get("X-Riesz-Download-Token") or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        return None
    return expected


@require_http_methods(["GET"])
@api_view()
async def node_download_token(request: HttpRequest) -> HttpResponse:
    tok, pinned = _get_node_download_token()
    return ok({"token": tok, "pinned": pinned})


@require_http_methods(["POST"])
@api_view()
async def node_download_token_rotate(request: HttpRequest) -> HttpResponse:
    tok = _rotate_node_download_token()
    return ok({"token": tok, "pinned": False})


@csrf_exempt
@require_http_methods(["GET"])
async def node_client_zip(request: HttpRequest) -> HttpResponse:
    """Serve installable `riesz-node.zip` archive (hidden behind secret download token)."""
    if not _check_download_token(request):
        return HttpResponse("Not Found", status=404, content_type="text/plain")
    try:
        resp = await runner.request("GET", "/v1/nodes/client.zip", timeout=60)
    except runner.RunnerUnavailable:
        return _unavailable()
    async with resp:
        data = await resp.read()
        out = HttpResponse(data, status=resp.status, content_type=resp.headers.get("Content-Type", "application/zip"))
        for hdr in ("Content-Disposition", "X-Riesz-Client-Hash"):
            if resp.headers.get(hdr):
                out[hdr] = resp.headers[hdr]
        out["Cache-Control"] = "no-store"
        return out


@csrf_exempt
@require_http_methods(["GET"])
async def node_bootstrap_py(request: HttpRequest) -> HttpResponse:
    """Serve zero-config cross-platform Python launcher (hidden behind secret download token)."""
    tok = _check_download_token(request)
    if not tok:
        return HttpResponse("Not Found", status=404, content_type="text/plain")
    origin = (getattr(settings, "PUBLIC_ORIGIN", "") or "").rstrip("/")
    if not origin:
        proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
        origin = f"{proto}://{request.get_host()}"
    try:
        resp = await runner.request("GET", "/v1/nodes/bootstrap.py", params={"origin": origin, "token": tok}, timeout=30)
    except runner.RunnerUnavailable:
        return _unavailable()
    async with resp:
        out = HttpResponse(await resp.read(), status=resp.status,
                           content_type=resp.headers.get("Content-Type", "text/x-python"))
        out["Cache-Control"] = "no-store"
        return out


@csrf_exempt
@require_http_methods(["GET", "POST"])
async def node_transfer(request: HttpRequest, token: str) -> HttpResponse:
    """Single-use token-authenticated streaming file transfer between runner and `riesz-node`."""
    target = f"/v1/nodes/transfer/{token}"
    if request.method == "GET":
        try:
            resp = await runner.request("GET", target, timeout=600)
        except runner.RunnerUnavailable:
            return _unavailable()
        if resp.status != 200:
            async with resp:
                return HttpResponse(await resp.read(), status=resp.status, content_type="application/json")

        async def chunks():
            try:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    yield chunk
            finally:
                resp.release()

        out = StreamingHttpResponse(chunks(), status=200, content_type="application/octet-stream")
        for hdr in ("Content-Length", "X-Riesz-SHA256"):
            if resp.headers.get(hdr):
                out[hdr] = resp.headers[hdr]
        out["Cache-Control"] = "no-store"
        return out

    headers = {}
    if request.headers.get("X-Riesz-SHA256"):
        headers["X-Riesz-SHA256"] = request.headers["X-Riesz-SHA256"]
    if request.headers.get("Content-Length"):
        headers["Content-Length"] = request.headers["Content-Length"]

    async def upload_stream():
        while True:
            chunk = await asyncio.to_thread(request.read, 256 * 1024)
            if not chunk:
                break
            yield chunk

    try:
        resp = await runner.request("POST", target, data=upload_stream(), headers=headers or None, timeout=600)
    except runner.RunnerUnavailable:
        return _unavailable()
    async with resp:
        return HttpResponse(await resp.read(), status=resp.status, content_type="application/json")
