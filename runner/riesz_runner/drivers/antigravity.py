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

# What `agy acp` offers (checked 11-20 with a Google sign-in): default, accept-edits, auto and plan.
# Its plan mode is not read-only: it plans first, then works and asks like default. Auto needs its
# terminal sandbox: agy leaves it out while kernel.apparmor_restrict_unprivileged_userns = 1 (the
# agent's own list is what a session shows). With no mode that approves everything, the ACP driver
# adds Riesz Manager's BYPASS.
MODES = [
    Mode("default", "逐项确认", "ask", "编辑文件和运行命令前都先问你"),
    Mode("accept-edits", "自动接受编辑", "edits", "工作区里的编辑自动通过，命令仍要确认"),
    Mode("auto", "自动", "auto", "编辑和安全的命令自动执行，不可逆的操作先问你（要用到它的终端沙箱）"),
    Mode("plan", "先计划", "ask", "先仔细规划再动手；编辑和命令照常要你确认"),
]
MODE_NAMES = {m.id: m.name for m in MODES}
MODE_DESCRIPTIONS = {m.id: m.description for m in MODES}


class AntigravityDriver(AcpDriver):
    harness = "antigravity"
    capabilities = Capabilities(queue=True, approve_for_session=True, model_switch="next-turn", effort_switch="none")
    default_modes = MODES

    def __init__(self, *, agy: str = "agy", api: str | None = None, **kwargs: Any) -> None:
        super().__init__(argv=[agy, "acp", "--disable-telemetry"], **kwargs)
        self.api = api or "gemini:subscription"
        self.mode = self.mode or "default"

    def mode_name(self, mode: dict[str, Any]) -> str:
        return MODE_NAMES.get(str(mode.get("id")), super().mode_name(mode))

    def mode_description(self, mode: dict[str, Any]) -> str:
        return MODE_DESCRIPTIONS.get(str(mode.get("id")), super().mode_description(mode))

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
