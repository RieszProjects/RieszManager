"""Persistent security state: TOTP enrolment, recovery codes and login audit log."""

from __future__ import annotations

from django.db import models


class TotpDevice(models.Model):
    """The single TOTP authenticator of the (only) account. The secret is stored encrypted."""

    username = models.CharField(max_length=150, unique=True)
    encrypted_secret = models.TextField()
    confirmed_at = models.DateTimeField()
    last_used_step = models.BigIntegerField(default=0)


class RecoveryCode(models.Model):
    username = models.CharField(max_length=150, db_index=True)
    code_hash = models.CharField(max_length=256)
    used_at = models.DateTimeField(null=True, blank=True)


class LoginAttempt(models.Model):
    STAGE_PASSWORD = "password"
    STAGE_TOTP = "totp"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ip = models.CharField(max_length=64, db_index=True)
    username = models.CharField(max_length=150, blank=True)
    stage = models.CharField(max_length=16)
    success = models.BooleanField(default=False)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]


class SessionInfo(models.Model):
    """Metadata about a logged-in browser session (for the "devices" list)."""

    session_key = models.CharField(max_length=64, unique=True)
    username = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    ip = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
