"""Browsers that receive push notifications (one row per device/browser)."""

from __future__ import annotations

from django.db import models


class PushSubscription(models.Model):
    # The push service URL is unique per browser installation and identifies it.
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    failures = models.IntegerField(default=0)

    def info(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}
