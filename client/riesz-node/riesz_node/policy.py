"""Client-side local security policy (enforced ON the personal computer).

Even if the remote server were compromised, this module enforces:
- Strict directory allowlist containment via realpath (blocks `..` and symlink escapes).
- Hard sensitive-path blocklist (SSH keys, cloud creds, browser cookies, riesz-node keys).
- Read-only mode command restriction.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

SENSITIVE_PATH_RE = re.compile(
    r"(^|[\s\"'=:/\\])(\.ssh|\.aws|\.gnupg|\.kube|riesz-node|id_rsa|id_ed25519|/etc/shadow|/etc/passwd|"
    r"Login Data|Cookies|Keychain|AppData[/\\]Local[/\\]Google[/\\]Chrome[/\\]User Data|"
    r"\.docker[/\\]config\.json|\.npmrc|\.pypirc|\.netrc)(\b|[/\\]|$)",
    re.I,
)

READONLY_CMDS = {
    "ls", "dir", "pwd", "cd", "echo", "printf", "cat", "head", "tail", "wc", "stat", "file",
    "which", "where", "whoami", "hostname", "uname", "date", "uptime", "env", "printenv",
    "rg", "grep", "egrep", "fgrep", "ag", "fd", "tree", "du", "df", "ps", "lsblk",
    "get-childitem", "get-content", "get-location", "select-string", "test-path",
}
READONLY_GIT_SUBS = {"status", "diff", "log", "show", "branch", "tag", "rev-parse", "ls-files", "remote"}
SHELL_META_WRITE = re.compile(r"(>>?|<<|\$\(|`|;\s*|&&|\|\|)")


class PolicyError(Exception):
    """Raised when a requested path or command violates local hard boundaries."""


def normalize_roots(dirs: list[str]) -> list[Path]:
    roots: list[Path] = []
    for d in dirs:
        p = Path(os.path.realpath(os.path.expanduser(d)))
        if not p.is_dir():
            raise PolicyError(f"允许目录不存在：{d}")
        if p not in roots:
            roots.append(p)
    if not roots:
        raise PolicyError("必须通过 --dir 至少指定一个允许操作的目录")
    return roots


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        if os.name == "nt":
            p_s = os.path.normcase(str(path))
            r_s = os.path.normcase(str(root))
            return p_s == r_s or p_s.startswith(r_s.rstrip("\\/") + os.sep)
        return False


def resolve_allowed_path(raw: str, roots: list[Path], *, must_exist: bool = False) -> Path:
    """Resolve `raw` inside `roots` (relative paths anchor to `roots[0]`), blocking symlink escapes and secrets."""
    text = (raw or "").strip()
    if not text:
        return roots[0]
    p = Path(os.path.expanduser(text))
    if not p.is_absolute():
        p = roots[0] / p
    # Resolve realpath for the deepest existing ancestor so non-existent destinations still check symlinks.
    if p.exists():
        resolved = Path(os.path.realpath(p))
    else:
        parent = p.parent
        suffix_parts = [p.name]
        while not parent.exists() and parent != parent.parent:
            suffix_parts.append(parent.name)
            parent = parent.parent
        resolved = Path(os.path.realpath(parent)).joinpath(*reversed(suffix_parts))
    if SENSITIVE_PATH_RE.search(str(resolved)) or SENSITIVE_PATH_RE.search(text):
        raise PolicyError(f"禁止访问敏感凭证路径：{raw}")
    if not any(_is_under(resolved, root) for root in roots):
        raise PolicyError(f"路径超出本地 --dir 白名单范围：{resolved}")
    if must_exist and not resolved.exists():
        raise PolicyError(f"本地文件不存在：{resolved}")
    return resolved


def is_readonly_command(command: str) -> bool:
    text = command.strip()
    if not text or "\n" in text or SENSITIVE_PATH_RE.search(text) or SHELL_META_WRITE.search(text):
        return False
    segments = [seg.strip() for seg in text.split("|")]
    if not segments or len(segments) > 3:
        return False
    for seg in segments:
        if not seg:
            return False
        try:
            tokens = shlex.split(seg, posix=os.name != "nt")
        except ValueError:
            return False
        if not tokens:
            return False
        cmd = os.path.basename(tokens[0]).lower()
        if cmd.endswith(".exe"):
            cmd = cmd[:-4]
        if cmd in READONLY_CMDS:
            continue
        if cmd == "git" and len(tokens) >= 2 and tokens[1].lower() in READONLY_GIT_SUBS:
            if any(t in ("-D", "-d", "--delete", "--force", "-f") for t in tokens[2:]):
                return False
            continue
        if cmd == "find":
            if any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for t in tokens[1:]):
                return False
            continue
        return False
    return True


def check_command_policy(command: str, cwd: str, roots: list[Path], mode: str) -> Path:
    """Validate cwd containment and local mode constraints; returns resolved safe cwd."""
    safe_cwd = resolve_allowed_path(cwd, roots, must_exist=True)
    if not safe_cwd.is_dir():
        raise PolicyError(f"工作目录不是文件夹：{safe_cwd}")
    if SENSITIVE_PATH_RE.search(command):
        raise PolicyError("本地硬安全策略拒绝：命令涉及敏感凭证路径")
    if mode == "readonly" and not is_readonly_command(command):
        raise PolicyError("本地策略拒绝：当前绑定为只读模式，仅允许只读查询命令")
    return safe_cwd
