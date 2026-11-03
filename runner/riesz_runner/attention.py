"""Moments worth telling the user about when they are not looking.

The hub turns these into global `attention` events; the backend relays them
as push notifications. Kinds:

- `permission`: a session waits for a tool permission (or plan approval);
- `question`: Claude asked the user something (AskUserQuestion);
- `done`: a turn that ran for at least LONG_TURN_SECONDS finished;
- `exited`: a hosted claude process ended unexpectedly.
"""

from __future__ import annotations

import os
from typing import Any

LONG_TURN_SECONDS = 60
MAX_DETAIL = 160


def _clip(text: str, limit: int = MAX_DETAIL) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_request(req: dict[str, Any]) -> tuple[str, str]:
    """(kind, one-line description) of a can_use_tool request."""
    tool = str(req.get("toolName") or "")
    data = req.get("input") if isinstance(req.get("input"), dict) else {}
    if tool == "AskUserQuestion":
        questions = data.get("questions") or []
        first = questions[0] if questions and isinstance(questions[0], dict) else {}
        return "question", _clip(first.get("question") or first.get("header") or "Claude 有问题想问你")
    if tool == "ExitPlanMode":
        return "permission", "计划已写好，等你审批"
    if tool == "Bash":
        return "permission", f"运行命令：{_clip(data.get('command') or '', 120)}"
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        path = data.get("file_path") or data.get("notebook_path") or ""
        verb = "写入" if tool == "Write" else "修改"
        return "permission", f"{verb}文件：{os.path.basename(str(path)) or path}"
    if tool == "WebFetch":
        return "permission", f"访问网页：{_clip(data.get('url') or '', 120)}"
    label = req.get("title") or req.get("displayName") or tool or "工具"
    return "permission", f"使用 {_clip(label, 80)}"


def session_title(summary: dict[str, Any]) -> str:
    for key in ("customTitle", "title", "firstPrompt"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value, 60)
    return "会话"
