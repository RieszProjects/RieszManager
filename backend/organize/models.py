"""User-defined organisation: archive flags and manual order of sessions and
projects, and the quick phrases offered by the mascot doll.

Sessions are identified by Claude Code's session UUID, projects by the runner's
project id (a folder name, "~root", or "ext:<encoded path>"). Nothing here is
required for a session or project to exist; rows are created on first change.
"""

from __future__ import annotations

from django.db import models


class ItemState(models.Model):
    KIND_PROJECT = "project"
    KIND_SESSION = "session"
    KINDS = [(KIND_PROJECT, "project"), (KIND_SESSION, "session")]

    kind = models.CharField(max_length=16, choices=KINDS)
    key = models.CharField(max_length=700)
    archived_at = models.DateTimeField(null=True, blank=True)
    # Manual order within the item's list (projects: the sidebar; sessions: their project).
    position = models.IntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "key"], name="unique_item_state")]


class QuickPhrase(models.Model):
    """A snippet the doll widget can drop into the composer, in the user's order."""

    text = models.TextField()
    position = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
