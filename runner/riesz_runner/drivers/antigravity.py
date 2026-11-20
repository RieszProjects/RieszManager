"""Antigravity CLI sessions, through its ACP mode (`agy acp`).

agy signs in with the user's Google account (a Google AI subscription; run
`agy acp --login` once in a terminal) or a Gemini API key. Only the official,
unmodified agy runs here and it keeps its own sign-in: this driver never reads
or passes its tokens. Telemetry is turned off with --disable-telemetry.
"""

from __future__ import annotations

from typing import Any

from .acp import AcpDriver
from .base import Capabilities, Mode
from .jsonrpc import JsonRpcError

MODES = [
    Mode("default", "逐项确认", "编辑文件和运行命令前都先问你"),
    Mode("accept-edits", "自动接受编辑", "编辑文件自动通过，命令仍需确认"),
    Mode("auto", "自动", "由 Antigravity 判断，安全的操作自动执行"),
    Mode("plan", "计划模式", "只读，先给出计划"),
]
MODE_NAMES = {m.id: m.name for m in MODES} | {"accept_edits": "自动接受编辑", "acceptEdits": "自动接受编辑"}


class AntigravityDriver(AcpDriver):
    harness = "antigravity"
    capabilities = Capabilities(queue=True, approve_for_session=True, model_switch="next-turn", effort_switch="none")
    default_modes = MODES

    def __init__(self, *, agy: str = "agy", api: str | None = None, **kwargs: Any) -> None:
        super().__init__(argv=[agy, "acp", "--disable-telemetry"], **kwargs)
        self.api = api or "gemini:subscription"

    def mode_name(self, mode: dict[str, Any]) -> str:
        return MODE_NAMES.get(str(mode.get("id")), super().mode_name(mode))

    async def authenticate(self, error: JsonRpcError) -> bool:
        if "auth" not in error.message.lower():
            return False
        method = "gemini-api-key" if self.api == "gemini:key" else "oauth-personal"
        assert self.peer is not None
        try:
            # With a cached sign-in (or the key in GEMINI_API_KEY) this returns at once.
            await self.peer.request("authenticate", {"methodId": method}, timeout=20)
        except Exception as exc:  # noqa: BLE001 - a browser sign-in cannot happen here
            if method == "oauth-personal":
                raise JsonRpcError(-32000, "not signed in: run `agy acp --login` in a terminal") from exc
            raise
        return True
