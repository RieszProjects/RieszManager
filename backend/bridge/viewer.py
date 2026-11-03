"""Rendered previews of project HTML files, isolated from the app.

A preview is served under a path-based URL, /api/view/<token>/<path>, so that
relative links inside the page (CSS, images, scripts, other pages) resolve to
neighbouring project files. The token is a short-lived signed capability for
reading one project's files; it replaces the session cookie because sandboxed
documents do not send it.

Every response carries a CSP `sandbox` (without allow-same-origin): the page and
its scripts run in an opaque origin and cannot use the app's cookies, storage or
API, even when opened in a tab of its own.
"""

from __future__ import annotations

from urllib.parse import quote

from django.core import signing
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

from riesz.http import ApiError, api_view, error_response, ok

from . import runner

SALT = "riesz-file-view"
TOKEN_SECONDS = 3600
SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox; "
    "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'; frame-ancestors 'self'"
)


def make_token(project: str) -> str:
    return signing.dumps({"p": project}, salt=SALT, compress=True)


@require_GET
@api_view()
def view_token(request: HttpRequest) -> HttpResponse:
    project = request.GET.get("project") or ""
    if not project or len(project) > 700:
        raise ApiError(400, "缺少项目")
    token = make_token(project)
    return ok({"base": f"/api/view/{quote(token, safe='')}/", "expiresIn": TOKEN_SECONDS})


@require_GET
async def serve(request: HttpRequest, token: str, path: str) -> HttpResponse:
    try:
        data = signing.loads(token, salt=SALT, max_age=TOKEN_SECONDS)
    except signing.BadSignature:
        return error_response(403, "预览链接已失效，请重新打开")
    project = data.get("p") if isinstance(data, dict) else None
    if not isinstance(project, str):
        return error_response(403, "预览链接无效")
    if not path or path.endswith("/"):
        path += "index.html"
    try:
        resp = await runner.request("GET", "/v1/files/raw", params={"project": project, "path": path}, timeout=600)
    except runner.RunnerUnavailable:
        return error_response(503, "宿主机运行器未连接")
    if resp.status != 200:
        async with resp:
            out = HttpResponse(await resp.read(), status=resp.status, content_type="application/json")
            out["Content-Security-Policy"] = SANDBOX_CSP
            return out

    async def body():
        try:
            async for chunk in resp.content.iter_chunked(1 << 16):
                yield chunk
        finally:
            resp.release()

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    if content_type.startswith("text/") and "charset" not in content_type:
        content_type += "; charset=utf-8"
    out = StreamingHttpResponse(body(), content_type=content_type)
    if resp.headers.get("Content-Length"):
        out["Content-Length"] = resp.headers["Content-Length"]
    out["Content-Security-Policy"] = SANDBOX_CSP
    out["Referrer-Policy"] = "no-referrer"  # the token is in the URL; never leak it to other sites
    out["X-Content-Type-Options"] = "nosniff"
    out["Cache-Control"] = "private, max-age=300"
    return out
