"""The user's Claude Code defaults: model/effort (settings.json) and Chrome integration (~/.claude.json)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def user_defaults(claude_home: Path, models: list[dict[str, Any]]) -> dict[str, Any]:
    """Return {"model", "resolvedModel", "effort"} as Claude Code would apply them to a new session."""
    try:
        settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    model = settings.get("model") or "default"
    resolved = None
    for m in models:
        if m.get("value") == model:
            resolved = m.get("resolvedModel")
            break
    resolved = resolved or model
    per_model = (settings.get("modelSettings") or {}).get(resolved) or {}
    effort = per_model.get("effortLevel") or settings.get("effortLevel")
    return {"model": model, "resolvedModel": resolved, "effort": effort}


def chrome_enabled_by_default(claude_home: Path) -> bool:
    """Whether interactive Claude Code would connect to Claude in Chrome by default.

    Interactive sessions honour `claudeInChromeDefaultEnabled` in ~/.claude.json
    (toggled with /chrome); print/stream-json sessions only do so with --chrome.
    """
    for candidate in (claude_home / ".claude.json", claude_home.parent / ".claude.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return bool(data.get("claudeInChromeDefaultEnabled"))
    return False
