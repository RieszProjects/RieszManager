"""Login, logout, TOTP enrolment and session management endpoints."""

from __future__ import annotations

import time

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.sessions.models import Session
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from riesz.http import ApiError, api_view, json_body, ok

from . import services
from .models import LoginAttempt, SessionInfo

FAILURE_DELAY_SECONDS = 0.6


def _throttle_or_raise(request: HttpRequest) -> None:
    state = services.throttle_state(services.client_ip(request))
    if state.blocked:
        raise ApiError(429, f"尝试次数过多，请 {state.retry_after} 秒后再试", "throttled",
                       retryAfter=state.retry_after)


def _complete_login(request: HttpRequest, remember: bool) -> JsonResponse:
    user = services.get_account_user()
    login(request, user)
    request.session.set_expiry(settings.SESSION_COOKIE_AGE if remember else 0)
    request.session["_seen"] = timezone.now().timestamp()
    request.session.save()
    SessionInfo.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={"username": user.username, "ip": services.client_ip(request),
                  "user_agent": services.user_agent(request)},
    )
    return ok(_state(request))


def _state(request: HttpRequest) -> dict:
    username = settings.RIESZ_USERNAME
    authenticated = request.user.is_authenticated
    pending = request.session.get("pending_login")
    pending_valid = bool(pending and time.time() - pending.get("t", 0) < settings.PENDING_LOGIN_SECONDS)
    totp_enabled = services.totp_device(username) is not None
    return {
        "authenticated": authenticated,
        "pendingTotp": (not authenticated) and pending_valid,
        "username": username if authenticated else None,
        "totpEnabled": totp_enabled if authenticated else None,
        "totpSetupRequired": authenticated and settings.RIESZ_REQUIRE_TOTP and not totp_enabled,
    }


@require_GET
@ensure_csrf_cookie
@api_view(auth=False)
def auth_state(request: HttpRequest) -> JsonResponse:
    return ok(_state(request))


@require_POST
@api_view(auth=False)
def auth_login(request: HttpRequest) -> JsonResponse:
    _throttle_or_raise(request)
    body = json_body(request)
    username = str(body.get("username") or "")[:150]
    password = str(body.get("password") or "")[:1024]
    remember = bool(body.get("remember", True))
    if not services.check_credentials(username, password):
        services.record_attempt(request, username, LoginAttempt.STAGE_PASSWORD, False)
        time.sleep(FAILURE_DELAY_SECONDS)
        raise ApiError(400, "用户名或密码错误", "bad_credentials")
    services.record_attempt(request, username, LoginAttempt.STAGE_PASSWORD, True)
    if services.totp_device(settings.RIESZ_USERNAME) is not None:
        request.session.cycle_key()
        request.session["pending_login"] = {"t": time.time(), "remember": remember}
        return ok({**_state(request), "pendingTotp": True})
    return _complete_login(request, remember)


@require_POST
@api_view(auth=False)
def auth_totp(request: HttpRequest) -> JsonResponse:
    pending = request.session.get("pending_login")
    if not pending or time.time() - pending.get("t", 0) > settings.PENDING_LOGIN_SECONDS:
        request.session.pop("pending_login", None)
        raise ApiError(400, "登录已过期，请重新输入密码", "pending_expired")
    _throttle_or_raise(request)
    body = json_body(request)
    code = str(body.get("code") or "")[:64]
    username = settings.RIESZ_USERNAME
    if body.get("recovery"):
        valid = services.use_recovery_code(username, code)
    else:
        valid = services.verify_totp(username, code)
    if not valid:
        services.record_attempt(request, username, LoginAttempt.STAGE_TOTP, False)
        time.sleep(FAILURE_DELAY_SECONDS)
        raise ApiError(400, "验证码无效", "bad_code")
    services.record_attempt(request, username, LoginAttempt.STAGE_TOTP, True)
    request.session.pop("pending_login", None)
    return _complete_login(request, bool(pending.get("remember", True)))


@require_POST
@api_view(auth=False)
def auth_logout(request: HttpRequest) -> JsonResponse:
    key = request.session.session_key
    logout(request)
    if key:
        SessionInfo.objects.filter(session_key=key).delete()
    return ok(_state(request))


# ---- account security --------------------------------------------------------

def _prune_sessions() -> None:
    live = set(Session.objects.filter(expire_date__gt=timezone.now()).values_list("session_key", flat=True))
    SessionInfo.objects.exclude(session_key__in=live).delete()


@require_GET
@api_view(allow_totp_setup=True)
def security_overview(request: HttpRequest) -> JsonResponse:
    _prune_sessions()
    username = request.user.username
    current = request.session.session_key
    sessions = [
        {"id": s.id, "current": s.session_key == current, "createdAt": s.created_at.isoformat(),
         "lastSeen": s.last_seen.isoformat(), "ip": s.ip, "userAgent": s.user_agent}
        for s in SessionInfo.objects.filter(username=username).order_by("-last_seen")
    ]
    attempts = [
        {"at": a.created_at.isoformat(), "ip": a.ip, "stage": a.stage, "success": a.success,
         "userAgent": a.user_agent}
        for a in LoginAttempt.objects.all()[:30]
    ]
    return ok({
        "username": username,
        "totpEnabled": services.totp_device(username) is not None,
        "totpRequired": settings.RIESZ_REQUIRE_TOTP,
        "recoveryCodesLeft": services.recovery_codes_left(username),
        "sessions": sessions,
        "attempts": attempts,
    })


@require_POST
@api_view(allow_totp_setup=True)
def totp_setup(request: HttpRequest) -> JsonResponse:
    secret = services.new_totp_secret()
    request.session["totp_setup"] = {"secret": secret, "t": time.time()}
    return ok({"secret": secret, "uri": services.provisioning_uri(secret, request.user.username)})


@require_POST
@api_view(allow_totp_setup=True)
def totp_confirm(request: HttpRequest) -> JsonResponse:
    pending = request.session.get("totp_setup")
    if not pending or time.time() - pending.get("t", 0) > 900:
        raise ApiError(400, "设置已过期，请重新开始", "setup_expired")
    code = str(json_body(request).get("code") or "")
    step = services.verify_totp_secret(pending["secret"], code)
    if step is None:
        raise ApiError(400, "验证码无效", "bad_code")
    codes = services.enable_totp(request.user.username, pending["secret"], step)
    request.session.pop("totp_setup", None)
    return ok({"recoveryCodes": codes})


def _require_second_factor(request: HttpRequest, body: dict) -> None:
    code = str(body.get("code") or "")
    username = request.user.username
    if body.get("recovery"):
        valid = services.use_recovery_code(username, code)
    else:
        valid = services.verify_totp(username, code)
    if not valid:
        raise ApiError(400, "验证码无效", "bad_code")


@require_POST
@api_view()
def totp_disable(request: HttpRequest) -> JsonResponse:
    if settings.RIESZ_REQUIRE_TOTP:
        raise ApiError(400, "当前配置要求必须启用两步验证", "totp_required")
    _require_second_factor(request, json_body(request))
    services.disable_totp(request.user.username)
    return ok()


@require_POST
@api_view()
def recovery_regenerate(request: HttpRequest) -> JsonResponse:
    _require_second_factor(request, json_body(request))
    return ok({"recoveryCodes": services.regenerate_recovery_codes(request.user.username)})


@require_POST
@api_view(allow_totp_setup=True)
def revoke_sessions(request: HttpRequest) -> JsonResponse:
    body = json_body(request)
    current = request.session.session_key
    infos = SessionInfo.objects.filter(username=request.user.username).exclude(session_key=current)
    if body.get("id") is not None:
        infos = infos.filter(id=int(body["id"]))
    keys = list(infos.values_list("session_key", flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    infos.delete()
    return ok({"revoked": len(keys)})
