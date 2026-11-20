"""Gemini CLI sessions, through its ACP mode (`gemini --acp`).

Gemini CLI signs in with a Gemini API key here (GEMINI_API_KEY): personal
Google accounts were moved to Antigravity CLI in June 2026, and Google's terms
rule out using a Gemini CLI login from other software. A small system settings
file (GEMINI_CLI_SYSTEM_SETTINGS_PATH, only for these processes) picks the API
key sign-in, hides key variables from the commands it runs and turns usage
statistics off; the user's own settings stay as they are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .acp import AcpDriver
from .base import Capabilities, Mode

MODES = [
    Mode("default", "逐项确认", "ask", "编辑文件和运行命令前都先问你"),
    Mode("autoEdit", "自动接受编辑", "edits", "编辑文件自动通过，命令仍需确认"),
    Mode("yolo", "全部自动", "full", "不再询问任何操作"),
    Mode("plan", "计划模式", "readonly", "只读，先给出计划"),
]
MODE_NAMES = {m.id: m.name for m in MODES}
DEFAULT_MODE = "default"

SETTINGS = {
    # Hide variables such as GEMINI_API_KEY from the commands it runs (off by default in Gemini CLI).
    "security": {"auth": {"selectedType": "gemini-api-key"}, "environmentVariableRedaction": {"enabled": True}},
    "privacy": {"usageStatisticsEnabled": False},
}


def settings_file(state_dir: Path) -> str:
    path = state_dir / "gemini" / "system-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(SETTINGS, indent=1)
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return str(path)


class GeminiDriver(AcpDriver):
    harness = "gemini"
    capabilities = Capabilities(queue=True, approve_for_session=True, model_switch="next-turn", effort_switch="none")
    default_modes = MODES

    def __init__(self, *, gemini: str = "gemini", settings: str | None = None, **kwargs: Any) -> None:
        super().__init__(argv=[gemini, "--acp", "--skip-trust"], **kwargs)
        self.mode = self.mode or DEFAULT_MODE
        self.settings = settings

    def _env(self) -> dict[str, str]:
        env = super()._env()
        if self.settings:
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = self.settings
        return env

    def mode_name(self, mode: dict[str, Any]) -> str:
        return MODE_NAMES.get(str(mode.get("id")), super().mode_name(mode))
