"""The user's Claude Code defaults (model and effort) from ~/.claude/settings.json."""

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
