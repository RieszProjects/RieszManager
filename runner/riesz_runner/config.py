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


def load_config(env_file: str | None = None) -> Config:
    env: dict[str, str] = {}
    if env_file:
        env.update(load_env_file(Path(env_file)))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("RIESZ_", "RUNNER_", "WORKSPACE_"))})

    home = Path.home()
    state_dir = Path(os.path.expanduser(env.get("RUNNER_STATE_DIR") or "~/.local/state/riesz-manager"))
    socket_path = Path(os.path.expanduser(env.get("RUNNER_SOCKET") or str(state_dir / "run" / "runner.sock")))
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
        extra_args=extra,
    )
