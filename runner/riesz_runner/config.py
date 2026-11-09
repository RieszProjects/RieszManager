"""Runner configuration, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file (KEY=VALUE, optional quotes, # comments).

    Single-quoted values are taken literally; double-quoted values support the
    usual backslash escapes. No variable interpolation is performed.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == '"':
            value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
        else:
            # Strip trailing inline comments on unquoted values.
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        values[key] = value
    return values


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _find_claude_binary() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude"):
        if candidate.exists():
            return str(candidate)
    return "claude"


def _inbound(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if value in ("accept", "hold", "refuse") else ""


@dataclass
class Config:
    workspace_root: Path
    claude_home: Path
    claude_bin: str
    state_dir: Path
    socket_path: Path
    token: str
    idle_timeout: int = 1800
    default_permission_mode: str = "default"
    allow_bypass: bool = False
    login_shell: bool = True
    allow_external_files: bool = False
    max_file_bytes: int = 2 * 1024 * 1024
    # Claude in Chrome for hosted sessions: "auto" follows the user's /chrome default, or "on"/"off".
    chrome: str = "auto"
    # Claude Code's crossSessionInbound for hosted sessions ("", "accept", "hold" or "refuse").
    # Empty leaves the user's own setting in charge.
    cross_session_inbound: str = ""
    # Other AI services whose balances the doll shows (see balances.py); empty: <state dir>/balances.json.
    balances_path: str = ""
    # In bypassPermissions mode, approve removals that stay inside project tmp folders (see autoapprove.py).
    allow_tmp_removal: bool = False
    # Answer a permission request or question nobody responded to after this many seconds (0: wait forever).
    permission_timeout: int = 1800
    extra_args: list[str] = field(default_factory=list)

    @property
    def projects_dir(self) -> Path:
        return self.claude_home / "projects"

    @property
    def live_sessions_dir(self) -> Path:
        return self.claude_home / "sessions"

    @property
    def history_file(self) -> Path:
        return self.claude_home / "history.jsonl"

    @property
    def uploads_dir(self) -> Path:
        return self.state_dir / "uploads"

    @property
    def own_history_file(self) -> Path:
        return self.state_dir / "history.jsonl"

    @property
    def balances_file(self) -> Path:
        return Path(os.path.expanduser(self.balances_path)) if self.balances_path else self.state_dir / "balances.json"


def _state_dir(env: dict[str, str]) -> Path:
    return Path(os.path.expanduser(env.get("RUNNER_STATE_DIR") or "~/.local/state/riesz-manager"))


def runner_socket(env: dict[str, str]) -> Path:
    """Path of the runner's Unix socket for these settings."""
    socket_dir = env.get("RUNNER_SOCKET_DIR")
    default = Path(os.path.expanduser(socket_dir)) / "runner.sock" if socket_dir else _state_dir(env) / "run" / "runner.sock"
    return Path(os.path.expanduser(env.get("RUNNER_SOCKET") or str(default)))


def load_config(env_file: str | None = None) -> Config:
    env: dict[str, str] = {}
    if env_file:
        env.update(load_env_file(Path(env_file)))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("RIESZ_", "RUNNER_", "WORKSPACE_"))})

    home = Path.home()
    state_dir = _state_dir(env)
    socket_path = runner_socket(env)
    token = env.get("RUNNER_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("RUNNER_TOKEN must be set to a random string of at least 24 characters")

    extra = env.get("RUNNER_CLAUDE_EXTRA_ARGS", "").split()
    return Config(
        workspace_root=Path(os.path.expanduser(env.get("WORKSPACE_ROOT") or str(home))).resolve(),
        claude_home=Path(os.path.expanduser(env.get("RUNNER_CLAUDE_HOME") or str(home / ".claude"))),
        claude_bin=env.get("RUNNER_CLAUDE_BIN") or _find_claude_binary(),
        state_dir=state_dir,
        socket_path=socket_path,
        token=token,
        idle_timeout=int(env.get("RUNNER_IDLE_TIMEOUT") or 1800),
        default_permission_mode=env.get("RUNNER_DEFAULT_PERMISSION_MODE") or "default",
        allow_bypass=_bool(env.get("RUNNER_ALLOW_BYPASS"), False),
        login_shell=_bool(env.get("RUNNER_LOGIN_SHELL"), True),
        allow_external_files=_bool(env.get("RUNNER_ALLOW_EXTERNAL_FILES"), False),
        max_file_bytes=int(env.get("RUNNER_MAX_FILE_BYTES") or 2 * 1024 * 1024),
        chrome=(env.get("RUNNER_CHROME") or "auto").strip().lower(),
        cross_session_inbound=_inbound(env.get("RUNNER_CROSS_SESSION_INBOUND")),
        balances_path=env.get("RUNNER_BALANCES_FILE") or "",
        allow_tmp_removal=_bool(env.get("RUNNER_ALLOW_TMP_REMOVAL"), False),
        permission_timeout=max(0, int(env.get("RUNNER_PERMISSION_TIMEOUT") or 1800)),
        extra_args=extra,
    )
