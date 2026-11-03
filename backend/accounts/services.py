"""Authentication logic: credential checks, TOTP, recovery codes and throttling."""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

import pyotp
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils import timezone

from .crypto import decrypt, encrypt
from .models import LoginAttempt, RecoveryCode, TotpDevice

RECOVERY_CODE_COUNT = 8


def client_ip(request: HttpRequest) -> str:
    if settings.RIESZ_TRUST_CF_HEADER:
        cf = request.META.get("HTTP_CF_CONNECTING_IP")
        if cf:
            return cf.strip()[:64]
    real = request.META.get("HTTP_X_REAL_IP")
    if real:
        return real.strip()[:64]
    return (request.META.get("REMOTE_ADDR") or "unknown")[:64]


def user_agent(request: HttpRequest) -> str:
    return (request.META.get("HTTP_USER_AGENT") or "")[:300]


# ---- credentials ---------------------------------------------------------------

def check_credentials(username: str, password: str) -> bool:
    """Constant-time check of the configured username/password."""
    user_ok = hmac.compare_digest(username.encode(), settings.RIESZ_USERNAME.encode())
    if settings.RIESZ_PASSWORD_HASH:
        pass_ok = check_password(password, settings.RIESZ_PASSWORD_HASH)
    else:
        pass_ok = hmac.compare_digest(password.encode(), settings.RIESZ_PASSWORD.encode())
    return user_ok and pass_ok


def get_account_user() -> User:
    user, created = User.objects.get_or_create(username=settings.RIESZ_USERNAME)
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


# ---- throttling ----------------------------------------------------------------

@dataclass
class ThrottleState:
    blocked: bool
    retry_after: int = 0


def throttle_state(ip: str) -> ThrottleState:
    window = timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
    since = timezone.now() - window
    failures = LoginAttempt.objects.filter(created_at__gte=since, success=False)
    ip_failures = failures.filter(ip=ip)
    if ip_failures.count() >= settings.LOGIN_MAX_FAILURES_PER_IP:
        oldest = ip_failures.order_by("-created_at")[settings.LOGIN_MAX_FAILURES_PER_IP - 1]
        retry = int((oldest.created_at + window - timezone.now()).total_seconds()) + 1
        return ThrottleState(True, max(retry, 1))
    if failures.count() >= settings.LOGIN_MAX_FAILURES_GLOBAL:
        return ThrottleState(True, 60)
    return ThrottleState(False)


def record_attempt(request: HttpRequest, username: str, stage: str, success: bool) -> None:
    LoginAttempt.objects.create(ip=client_ip(request), username=username[:150], stage=stage,
                                success=success, user_agent=user_agent(request))
    # Keep the audit log bounded.
    cutoff = timezone.now() - timedelta(days=90)
    LoginAttempt.objects.filter(created_at__lt=cutoff).delete()


# ---- TOTP ----------------------------------------------------------------------

def totp_device(username: str) -> TotpDevice | None:
    return TotpDevice.objects.filter(username=username).first()


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=settings.RIESZ_TOTP_ISSUER)


def verify_totp_secret(secret: str, code: str) -> int | None:
    """Return the matched time step if `code` is valid for `secret` (±1 step), else None."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for drift in (0, -1, 1):
        step = now // 30 + drift
        if hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


def verify_totp(username: str, code: str) -> bool:
    device = totp_device(username)
    if device is None:
        return False
    step = verify_totp_secret(decrypt(device.encrypted_secret), code)
    if step is None or step <= device.last_used_step:
        return False  # invalid, or a replay of an already used code
    device.last_used_step = step
    device.save(update_fields=["last_used_step"])
    return True


def enable_totp(username: str, secret: str, step: int) -> list[str]:
    TotpDevice.objects.update_or_create(
        username=username,
        defaults={"encrypted_secret": encrypt(secret), "confirmed_at": timezone.now(), "last_used_step": step},
    )
    return regenerate_recovery_codes(username)


def disable_totp(username: str) -> None:
    TotpDevice.objects.filter(username=username).delete()
    RecoveryCode.objects.filter(username=username).delete()


def regenerate_recovery_codes(username: str) -> list[str]:
    RecoveryCode.objects.filter(username=username).delete()
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(5)
        code = f"{raw[:5]}-{raw[5:]}"
        codes.append(code)
        RecoveryCode.objects.create(username=username, code_hash=make_password(code))
    return codes


def use_recovery_code(username: str, code: str) -> bool:
    normalized = code.strip().lower()
    if len(normalized) == 10 and "-" not in normalized:
        normalized = f"{normalized[:5]}-{normalized[5:]}"
    for rc in RecoveryCode.objects.filter(username=username, used_at__isnull=True):
        if check_password(normalized, rc.code_hash):
            rc.used_at = timezone.now()
            rc.save(update_fields=["used_at"])
            return True
    return False


def recovery_codes_left(username: str) -> int:
    return RecoveryCode.objects.filter(username=username, used_at__isnull=True).count()
