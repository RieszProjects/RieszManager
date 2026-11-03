"""Authenticated HTTP endpoints that forward to the host runner."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import aiohttp
from django.conf import settings
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_http_methods

from riesz.http import ApiError, api_view, error_response, ok

from . import runner

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# (method, public path regex, runner path template). Public paths are relative to /api/.
ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    (m, re.compile(p), t)
    for m, p, t in [
        ("GET", r"^projects$", "/v1/projects"),
        ("GET", r"^projects/(?P<pid>[^/]{1,512})$", "/v1/projects/{pid}"),
        ("GET", r"^sessions$", "/v1/sessions"),
        ("POST", r"^sessions$", "/v1/sessions"),
        ("GET", r"^live$", "/v1/live"),
        ("GET", r"^usage$", "/v1/usage"),
        ("GET", r"^commands$", "/v1/commands"),
        ("GET", rf"^sessions/(?P<sid>{UUID})$", "/v1/sessions/{sid}"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/items$", "/v1/sessions/{sid}/items"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/commands$", "/v1/sessions/{sid}/commands"),
        ("GET", rf"^sessions/(?P<sid>{UUID})/context$", "/v1/sessions/{sid}/context"),
        ("POST", rf"^sessions/(?P<sid>{UUID})/(?P<action>messages|start|stop|interrupt|cancel|model|mode|effort|rename|fork|takeover)$",
         "/v1/sessions/{sid}/{action}"),
        ("POST", rf"^sessions/(?P<sid>{UUID})/permissions/(?P<rid>[\w-]{{1,80}})$",
         "/v1/sessions/{sid}/permissions/{rid}"),
        ("GET", r"^history$", "/v1/history"),
        ("GET", r"^files/(?P<op>list|content|search)$", "/v1/files/{op}"),
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
        resp = await runner.request(request.method or "GET", target, params=list(request.GET.items()), json=body)
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
    try:
        resp = await runner.request("GET", target, params=list(request.GET.items()), timeout=600)
    except runner.RunnerUnavailable:
        return _unavailable()
    if resp.status != 200:
        async with resp:
            return HttpResponse(await resp.read(), status=resp.status, content_type="application/json")

    async def body():
        try:
            async for chunk in resp.content.iter_chunked(1 << 16):
                yield chunk
        finally:
            resp.release()

    out = StreamingHttpResponse(body(), content_type=resp.headers.get("Content-Type", "application/octet-stream"))
    if resp.headers.get("Content-Length"):
        out["Content-Length"] = resp.headers["Content-Length"]
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
    form = aiohttp.FormData()
    form.add_field("file", file.read(), filename=file.name, content_type=file.content_type or "application/octet-stream")
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
        "username": request.user.username,
    })
