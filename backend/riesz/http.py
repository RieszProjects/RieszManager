"""Small helpers shared by the JSON API views."""

from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable

from asgiref.sync import iscoroutinefunction, sync_to_async
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils import timezone


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.extra = extra


def error_response(status: int, message: str, code: str | None = None, **extra: Any) -> JsonResponse:
    payload = {"error": message, **extra}
    if code:
        payload["code"] = code
    return JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})


def ok(data: Any = None, status: int = 200) -> JsonResponse:
    return JsonResponse(data if data is not None else {"ok": True}, status=status, safe=False,
                        json_dumps_params={"ensure_ascii": False})


def json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError as exc:
        raise ApiError(400, "请求格式错误") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "请求格式错误")
    return data


def _auth_problem(request: HttpRequest) -> JsonResponse | None:
    """Return an error response if the request is not fully authenticated (sync)."""
    from accounts.models import SessionInfo
    from accounts.services import totp_device

    if not request.user.is_authenticated:
        return error_response(401, "未登录", "unauthenticated")
    if settings.RIESZ_REQUIRE_TOTP and getattr(request, "_riesz_allow_totp_setup", False) is False:
        if totp_device(request.user.username) is None:
            return error_response(403, "需要先设置两步验证", "totp_setup_required")
    # Touch the device record at most every few minutes.
    now = timezone.now().timestamp()
    if now - request.session.get("_seen", 0) > 300:
        request.session["_seen"] = now
        SessionInfo.objects.filter(session_key=request.session.session_key).update(last_seen=timezone.now())
    return None


def api_view(*, auth: bool = True, allow_totp_setup: bool = False) -> Callable:
    """Decorator for JSON API views (sync or async): auth check + ApiError handling."""

    def decorator(view: Callable) -> Callable:
        if iscoroutinefunction(view):
            @wraps(view)
            async def async_wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
                if auth:
                    request._riesz_allow_totp_setup = allow_totp_setup  # type: ignore[attr-defined]
                    await request.auser()
                    problem = await sync_to_async(_auth_problem)(request)
                    if problem is not None:
                        return problem
                try:
                    return await view(request, *args, **kwargs)
                except ApiError as exc:
                    return error_response(exc.status, exc.message, exc.code, **exc.extra)
            return async_wrapper

        @wraps(view)
        def sync_wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            if auth:
                request._riesz_allow_totp_setup = allow_totp_setup  # type: ignore[attr-defined]
                problem = _auth_problem(request)
                if problem is not None:
                    return problem
            try:
                return view(request, *args, **kwargs)
            except ApiError as exc:
                return error_response(exc.status, exc.message, exc.code, **exc.extra)
        return sync_wrapper

    return decorator
