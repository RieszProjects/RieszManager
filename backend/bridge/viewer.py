"""Rendered previews of project HTML files, isolated from the app.

A preview is served under a path-based URL, /api/view/<token>/<path>, so that
relative links inside the page (CSS, images, scripts, fonts, other pages)
resolve to neighbouring project files. The token is a short-lived signed
capability for reading one project's files (or one explicitly referenced local
asset); it replaces the session cookie because sandboxed documents do not send
it.

Every response carries a CSP `sandbox` (without allow-same-origin): the page and
its scripts run in an opaque origin and cannot use the app's cookies, storage or
API, even when opened in a tab of its own. Because opaque-origin documents send
`Origin: null` on CORS requests (ES modules, `fetch()`, `@font-face`, WASM,
WebGL textures), preview responses carry `Access-Control-Allow-Origin: *` so
local assets load cleanly while keeping app isolation intact.
"""

from __future__ import annotations

import html
import posixpath
import re
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

from django.core import signing
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET, require_http_methods

from riesz.http import ApiError, api_view, error_response, ok

from . import runner

SALT = "riesz-file-view"
TOKEN_SECONDS = 3600
MAX_REWRITE_BYTES = 16 * 1024 * 1024
SANDBOX_CSP = (
    "sandbox allow-scripts allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads; "
    "default-src * data: blob: 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'; frame-ancestors 'self'"
)

_SENSITIVE_DIRS = {".git", ".ssh", ".gnupg", ".aws", ".claude", ".codex", ".config", ".kube", ".docker"}
_SENSITIVE_PREFIXES = ("/etc", "/proc", "/sys", "/root", "/dev", "/var/run")
_SENSITIVE_EXTS = {".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx"}
_SENSITIVE_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}

_URL_ATTRS = {"src", "href", "poster", "data", "xlink:href"}

# HTML scanner: skips comments, <textarea>, <xmp>, and <script> bodies while rewriting tag attributes and <style>.
_HTML_TOKEN = re.compile(
    r"(?P<comment><!--[\s\S]*?-->)"
    r"|(?P<script><script\b(?P<script_attrs>[^>]*)>(?P<script_body>[\s\S]*?)</script\s*>)"
    r"|(?P<style><style\b(?P<style_attrs>[^>]*)>(?P<style_body>[\s\S]*?)</style\s*>)"
    r"|(?P<raw><(?:textarea|xmp)\b[^>]*>[\s\S]*?</(?:textarea|xmp)\s*>)"
    r"|(?P<tag><(?P<tag_name>[A-Za-z][A-Za-z0-9:-]*)(?P<attrs>\s[^>]*?)?(?P<self_close>/?)>)",
    re.IGNORECASE,
)

_ATTR_RE = re.compile(
    r"""(?P<prefix>\s+)(?P<name>[A-Za-z_:][\w:.-]*)(?P<eq>\s*=\s*)(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<uq>[^\s"'=<>`]+))""",
)

# CSS scanner: skips /* ... */ comments, rewrites url(...) and @import "...".
_CSS_TOKEN = re.compile(
    r"(?P<comment>/\*[\s\S]*?\*/)"
    r"|(?P<url>url\(\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<uq>[^)'\"\s]+))\s*\))"
    r"|(?P<imp>@import\s+(?:\"(?P<idq>[^\"]*)\"|'(?P<isq>[^']*)'))",
    re.IGNORECASE,
)


def make_token(project: str) -> str:
    return signing.dumps({"p": project}, salt=SALT, compress=True)


def make_file_token(project: str, rel: str) -> str:
    return signing.dumps({"p": project, "f": rel}, salt=SALT, compress=True)


def make_asset_token(abs_path: str) -> str:
    return signing.dumps({"abs": abs_path}, salt=SALT, compress=True)


def _encode_rel(rel: str) -> str:
    return "/".join(quote(seg, safe="") for seg in rel.lstrip("/").split("/") if seg)


def _join_url(path: str, query: str, fragment: str) -> str:
    out = path
    if query:
        out += "?" + query
    if fragment:
        out += "#" + fragment
    return out


def _is_sensitive_fs(fs_path: str) -> bool:
    for prefix in _SENSITIVE_PREFIXES:
        if fs_path == prefix or fs_path.startswith(prefix + "/"):
            return True
    parts = [p for p in fs_path.split("/") if p]
    if any(p in _SENSITIVE_DIRS for p in parts):
        return True
    name = parts[-1] if parts else ""
    if name in _SENSITIVE_NAMES or name.startswith(".env.") or name.startswith(("id_rsa", "id_ed25519")):
        return True
    _, dot, ext = name.rpartition(".")
    return bool(dot) and (f".{ext.lower()}" in _SENSITIVE_EXTS)


def _build_resolver(
    base_url: str | None,
    project_root: str,
    file_path: str,
    workspace_root: str,
    view_prefix: str,
) -> Callable[[str], str]:
    proj = project_root.rstrip("/") if project_root else ""
    ws = workspace_root.rstrip("/") if workspace_root else ""
    file_dir = posixpath.dirname(file_path) if file_path else ""
    prefix = view_prefix.strip("/") if view_prefix else ""
    rel_dir = ""
    if proj and file_dir and (file_dir == proj or file_dir.startswith(proj + "/")):
        rel_dir = file_dir[len(proj):].lstrip("/")

    def fs_to_view(fs_path: str, query: str, fragment: str) -> str | None:
        norm = posixpath.normpath(fs_path)
        if not norm.startswith("/") or _is_sensitive_fs(norm):
            return None
        if proj and base_url and (norm == proj or norm.startswith(proj + "/")):
            rel = norm[len(proj):].lstrip("/")
            full_rel = f"{prefix}/{rel}" if prefix else rel
            return _join_url(base_url + _encode_rel(full_rel), query, fragment)
        if ws and norm.startswith(ws + "/"):
            rel_ws = norm[len(ws) + 1:]
            p_name, sep, sub = rel_ws.partition("/")
            tok = make_file_token(p_name, sub) if sep else make_file_token("~root", p_name)
            name = quote(posixpath.basename(norm) or "file", safe="")
            return _join_url(f"/api/view/{quote(tok, safe='')}/{name}", query, fragment)
        tok = make_asset_token(norm)
        name = quote(posixpath.basename(norm) or "file", safe="")
        return _join_url(f"/api/view/{quote(tok, safe='')}/{name}", query, fragment)

    def resolve(raw: str) -> str:
        s = raw.strip()
        if not s or s.startswith(("#", "//", "/api/view/")):
            return raw
        parsed = urlsplit(s)
        scheme = parsed.scheme.lower()
        if scheme == "file":
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                return raw
            fs_path = unquote(parsed.path)
            return fs_to_view(fs_path, parsed.query, parsed.fragment) or raw
        if scheme:
            # http:, https:, data:, blob:, mailto:, tel:, javascript:, etc.
            return raw
        if parsed.netloc:
            return raw
        p = parsed.path
        if not p:
            return raw
        if p.startswith("/"):
            decoded = unquote(p)
            if (proj and decoded.startswith(proj + "/")) or (ws and decoded.startswith(ws + "/")):
                return fs_to_view(decoded, parsed.query, parsed.fragment) or raw
            if decoded.startswith(("/tmp/", "/home/", "/Users/", "/var/tmp/")):
                return fs_to_view(decoded, parsed.query, parsed.fragment) or raw
            if base_url:
                rel = decoded.lstrip("/")
                full_rel = f"{prefix}/{rel}" if prefix else rel
                q = parsed.query
                if rel_dir and "__rz_from=" not in q:
                    extra = f"__rz_from={quote(rel_dir, safe='/')}"
                    q = f"{q}&{extra}" if q else extra
                return _join_url(base_url + _encode_rel(full_rel), q, parsed.fragment)
            return raw
        # Relative path.
        if not file_dir:
            return raw
        if base_url is None:
            # Document itself was served from a single-file token; resolve relative refs to explicit tokens.
            target = posixpath.normpath(posixpath.join(file_dir, unquote(p)))
            return fs_to_view(target, parsed.query, parsed.fragment) or raw
        if ".." in p.split("/"):
            target = posixpath.normpath(posixpath.join(file_dir, unquote(p)))
            if proj and not (target == proj or target.startswith(proj + "/")):
                return fs_to_view(target, parsed.query, parsed.fragment) or raw
        return raw

    return resolve


def rewrite_css(css_text: str, resolve: Callable[[str], str]) -> str:
    def sub(m: re.Match[str]) -> str:
        if m.group("comment"):
            return m.group(0)
        if m.group("url"):
            if m.group("dq") is not None:
                val, q = m.group("dq"), '"'
            elif m.group("sq") is not None:
                val, q = m.group("sq"), "'"
            else:
                val, q = m.group("uq"), ""
            new = resolve(val)
            if new == val:
                return m.group(0)
            quote_char = q or '"'
            return f"url({quote_char}{new}{quote_char})"
        if m.group("imp"):
            if m.group("idq") is not None:
                val, q = m.group("idq"), '"'
            else:
                val, q = m.group("isq"), "'"
            new = resolve(val)
            if new == val:
                return m.group(0)
            return f"@import {q}{new}{q}"
        return m.group(0)

    return _CSS_TOKEN.sub(sub, css_text)


def _rewrite_srcset(value: str, resolve: Callable[[str], str]) -> str:
    parts = value.split(",")
    out: list[str] = []
    changed = False
    for part in parts:
        stripped = part.strip()
        if not stripped:
            out.append(part)
            continue
        pieces = stripped.split(None, 1)
        new_url = resolve(pieces[0])
        if new_url != pieces[0]:
            changed = True
            out.append(new_url + (f" {pieces[1]}" if len(pieces) > 1 else ""))
        else:
            out.append(stripped)
    return ", ".join(out) if changed else value


def _rewrite_attrs(attrs: str, resolve: Callable[[str], str]) -> str:
    if not attrs:
        return attrs

    def sub(m: re.Match[str]) -> str:
        name = m.group("name").lower()
        if name not in _URL_ATTRS and name not in ("srcset", "style"):
            return m.group(0)
        if m.group("dq") is not None:
            raw_val, q = m.group("dq"), '"'
        elif m.group("sq") is not None:
            raw_val, q = m.group("sq"), "'"
        else:
            raw_val, q = m.group("uq"), ""
        unescaped = html.unescape(raw_val)
        if name == "srcset":
            rewritten = _rewrite_srcset(unescaped, resolve)
        elif name == "style":
            rewritten = rewrite_css(unescaped, resolve)
        else:
            rewritten = resolve(unescaped)
        if rewritten == unescaped:
            return m.group(0)
        quote_char = q or '"'
        escaped = html.escape(rewritten, quote=True)
        return f"{m.group('prefix')}{m.group('name')}{m.group('eq')}{quote_char}{escaped}{quote_char}"

    return _ATTR_RE.sub(sub, attrs)


def rewrite_html(html_text: str, resolve: Callable[[str], str]) -> str:
    def sub(m: re.Match[str]) -> str:
        if m.group("comment") or m.group("raw"):
            return m.group(0)
        if m.group("script"):
            new_attrs = _rewrite_attrs(m.group("script_attrs") or "", resolve)
            return f"<script{new_attrs}>{m.group('script_body')}</script>"
        if m.group("style"):
            new_attrs = _rewrite_attrs(m.group("style_attrs") or "", resolve)
            new_body = rewrite_css(m.group("style_body") or "", resolve)
            return f"<style{new_attrs}>{new_body}</style>"
        if m.group("tag"):
            attrs = m.group("attrs") or ""
            new_attrs = _rewrite_attrs(attrs, resolve)
            if new_attrs == attrs:
                return m.group(0)
            return f"<{m.group('tag_name')}{new_attrs}{m.group('self_close')}>"
        return m.group(0)

    return _HTML_TOKEN.sub(sub, html_text)


def _apply_headers(out: HttpResponse | StreamingHttpResponse) -> HttpResponse | StreamingHttpResponse:
    out["Content-Security-Policy"] = SANDBOX_CSP
    out["Referrer-Policy"] = "no-referrer"  # the token is in the URL; never leak it to other sites
    out["X-Content-Type-Options"] = "nosniff"
    out["Access-Control-Allow-Origin"] = "*"
    out["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    out["Access-Control-Allow-Headers"] = "Range, If-Range"
    out["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
    out["Cross-Origin-Resource-Policy"] = "cross-origin"
    out["Cache-Control"] = "private, no-cache"
    return out


@require_GET
@api_view()
def view_token(request: HttpRequest) -> HttpResponse:
    project = request.GET.get("project") or ""
    if not project or len(project) > 700:
        raise ApiError(400, "缺少项目")
    token = make_token(project)
    return ok({"base": f"/api/view/{quote(token, safe='')}/", "expiresIn": TOKEN_SECONDS})


@require_http_methods(["GET", "HEAD", "OPTIONS"])
async def serve(request: HttpRequest, token: str, path: str) -> HttpResponse:
    if request.method == "OPTIONS":
        out = HttpResponse(status=204)
        out["Access-Control-Max-Age"] = "600"
        return _apply_headers(out)
    try:
        data = signing.loads(token, salt=SALT, max_age=TOKEN_SECONDS)
    except signing.BadSignature:
        return _apply_headers(error_response(403, "预览链接已失效，请重新打开"))
    if not isinstance(data, dict):
        return _apply_headers(error_response(403, "预览链接无效"))

    base_url: str | None = None
    if isinstance(data.get("abs"), str):
        endpoint = "/v1/files/view-asset"
        params: dict[str, str] = {"path": data["abs"]}
    elif isinstance(data.get("p"), str) and isinstance(data.get("f"), str):
        endpoint = "/v1/files/raw"
        params = {"project": data["p"], "path": data["f"]}
    elif isinstance(data.get("p"), str):
        project = data["p"]
        if not path or path.endswith("/"):
            path += "index.html"
        endpoint = "/v1/files/raw"
        params = {"project": project, "path": path}
        from_dir = request.GET.get("__rz_from")
        if from_dir:
            params["from"] = from_dir
        base_url = f"/api/view/{quote(token, safe='')}/"
    else:
        return _apply_headers(error_response(403, "预览链接无效"))

    method = "HEAD" if request.method == "HEAD" else "GET"
    forward = {h: request.headers[h] for h in ("Range", "If-Range") if h in request.headers}
    try:
        resp = await runner.request(method, endpoint, params=params, headers=forward or None, timeout=600)
    except runner.RunnerUnavailable:
        return _apply_headers(error_response(503, "宿主机运行器未连接"))

    if resp.status == 416:
        async with resp:
            out = HttpResponse(b"", status=416)
            if resp.headers.get("Content-Range"):
                out["Content-Range"] = resp.headers["Content-Range"]
            return _apply_headers(out)
    if resp.status not in (200, 206):
        async with resp:
            out = HttpResponse(await resp.read(), status=resp.status, content_type="application/json")
            return _apply_headers(out)

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    if content_type.startswith("text/") and "charset" not in content_type:
        content_type += "; charset=utf-8"
    mime_base = content_type.split(";", 1)[0].strip().lower()
    content_length = int(resp.headers["Content-Length"]) if resp.headers.get("Content-Length", "").isdigit() else None
    should_rewrite = (
        method == "GET"
        and resp.status == 200
        and mime_base in ("text/html", "text/css")
        and (content_length is None or content_length <= MAX_REWRITE_BYTES)
    )

    if should_rewrite:
        async with resp:
            raw_bytes = await resp.read()
        if len(raw_bytes) <= MAX_REWRITE_BYTES:
            project_root = unquote(resp.headers.get("X-Riesz-Project-Root") or "")
            file_path = unquote(resp.headers.get("X-Riesz-File-Path") or "")
            workspace_root = unquote(resp.headers.get("X-Riesz-Workspace-Root") or "")
            view_prefix = unquote(resp.headers.get("X-Riesz-View-Prefix") or "")
            resolve = _build_resolver(base_url, project_root, file_path, workspace_root, view_prefix)
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text = raw_bytes.decode("utf-8", errors="replace")
            rewritten = rewrite_html(text, resolve) if mime_base == "text/html" else rewrite_css(text, resolve)
            payload = rewritten.encode("utf-8")
        else:
            payload = raw_bytes
        out = HttpResponse(payload, status=200, content_type=content_type)
        out["Content-Length"] = str(len(payload))
        return _apply_headers(out)

    if method == "HEAD":
        async with resp:
            await resp.read()
        out = HttpResponse(b"", status=resp.status, content_type=content_type)
        for name in ("Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
            if resp.headers.get(name):
                out[name] = resp.headers[name]
        if resp.headers.get("Content-Length") and mime_base not in ("text/html", "text/css"):
            out["Content-Length"] = resp.headers["Content-Length"]
        return _apply_headers(out)

    async def body():
        try:
            async for chunk in resp.content.iter_chunked(1 << 16):
                yield chunk
        finally:
            resp.release()

    out = StreamingHttpResponse(body(), status=resp.status, content_type=content_type)
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
        if resp.headers.get(name):
            out[name] = resp.headers[name]
    return _apply_headers(out)
