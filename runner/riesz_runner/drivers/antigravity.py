"""Antigravity CLI sessions, through its ACP mode (`agy acp`).

agy signs in with the user's Google account (a Google AI subscription; run
`agy acp --login` once in a terminal) or a Gemini API key. Only the official,
unmodified agy runs here and it keeps its own sign-in: this driver never reads
or passes its tokens. Telemetry is turned off with --disable-telemetry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .acp import AcpDriver
from .base import Capabilities, Mode
from .jsonrpc import JsonRpcError

FILE_TOKEN_PATH = Path("~/.gemini/antigravity-cli/antigravity-oauth-token")
KEYRING_PATH = Path("~/.local/share/keyrings/Default_keyring.keyring")


def prefer_file_token(token_path: Path | None = None, keyring_path: Path | None = None) -> bool:
    """Whether `agy` should read its SSH/headless file OAuth token instead of GNOME Keyring.

    When signed in over SSH, `agy` stores credentials in `~/.gemini/antigravity-cli/antigravity-oauth-token`.
    Under `systemd --user` (where `SSH_CONNECTION` is unset), `agy` defaults to GNOME Keyring and would
    keep using a stale desktop login unless `SSH_CONNECTION` is present. Prefer the file token whenever
    it exists and is at least as recent as the GNOME keyring (or no keyring file exists).
    """
    tpath = (token_path or FILE_TOKEN_PATH).expanduser()
    try:
        tst = tpath.stat()
    except OSError:
        return False
    if tst.st_size == 0:
        return False
    kpath = (keyring_path or KEYRING_PATH).expanduser()
    try:
        kst = kpath.stat()
    except OSError:
        return True
    return tst.st_mtime >= kst.st_mtime


def agy_env(env: dict[str, str], *, token_path: Path | None = None, keyring_path: Path | None = None) -> dict[str, str]:
    """Adjust child env so `agy` picks the freshest OAuth credential store (file token vs desktop keyring)."""
    out = dict(env)
    if "SSH_CONNECTION" not in out and prefer_file_token(token_path, keyring_path):
        out["SSH_CONNECTION"] = "127.0.0.1 0 127.0.0.1 22"
    return out

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
    artifact_dirs = ("~/.gemini/antigravity-cli/brain/{id}",)       # implementation_plan.md, walkthroughs, ...
    capabilities = Capabilities(queue=True, approve_for_session=True, user_questions=True, model_switch="next-turn", effort_switch="none")
    default_modes = MODES

    def __init__(self, *, agy: str = "agy", api: str | None = None, **kwargs: Any) -> None:
        super().__init__(argv=[agy, "acp", "--disable-telemetry"], **kwargs)
        self.api = api or "gemini:subscription"
        self.mode = self.mode or "default"

    def _env(self) -> dict[str, str]:
        return agy_env(super()._env())

    def wakeup_after(self, name: str, inp: dict[str, Any]) -> float | None:
        """Its schedule tool ("Run schedule", DurationSeconds): it wakes itself up after that long."""
        if "schedule" not in name.lower():
            return None
        try:
            return float(inp.get("DurationSeconds"))
        except (TypeError, ValueError):
            return None

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
