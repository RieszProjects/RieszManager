"""Ironclad client-side local security policy (enforced ON the personal computer).

Even if the remote server or web account were compromised, this module enforces:
1. Strict directory allowlist containment (blocks `..`, non-existent path traversal,
   symlink/junction escapes, Windows UNC paths, ADS streams, reserved device names).
2. Unbypassable HARD_DENY rules (disk formatting, reboot/shutdown, fork bombs,
   credential/key theft, deleting roots or anything outside `--dir`).
3. Local high-risk confirmation rules (privilege escalation, remote pipe-to-shell,
   registry edits, dynamic/ambiguous deletions) that require confirmation on this computer.
4. Strict read-only command verification (no `env`/`printenv`, no out-of-bounds paths).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

SENSITIVE_PATH_RE = re.compile(
    r"(^|[\s\"'=:/\\])(\.ssh|\.aws|\.gnupg|\.kube|riesz-node|id_rsa|id_ed25519|/etc/shadow|/etc/passwd|"
    r"Login Data|Cookies|Keychain|AppData[/\\]Local[/\\]Google[/\\]Chrome[/\\]User Data|"
    r"Windows[/\\]System32[/\\]config[/\\](SAM|SYSTEM|SECURITY)|"
    r"\.docker[/\\]config\.json|\.npmrc|\.pypirc|\.netrc)(\b|[/\\]|$)",
    re.I,
)

# Never allowed in ANY mode (not even full or approved) — prevents irreversible machine destruction.
HARD_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|[;&|(\s])(mkfs(\.\w+)?|fdisk|parted|diskpart|bcdedit|grub-install|shutdown|reboot|poweroff|halt)\b", re.I),
     "禁止系统/磁盘/关机级破坏命令"),
    (re.compile(r"(^|[;&|(\s])format(\.com)?\s+[a-z]:", re.I), "禁止格式化磁盘分区"),
    (re.compile(r"(^|[;&|(\s])dd\b[^\n]*\bof\s*=\s*/dev/", re.I), "禁止直接写底层块设备"),
    (re.compile(r":\(\)\s*\{.*\|.*&\s*\}\s*;"), "禁止 Fork 炸弹"),
    (re.compile(r"\b(rm|rmdir|del|erase|rd|remove-item)\b[^\n]*(\s/\s*$|\s/\*|\b[A-Za-z]:\\(\s|$|\*)|\s~\s*$|\s~/\*|\s\$HOME\b)", re.I),
     "禁止删除系统根目录或家目录"),
]

# High-risk commands that MUST be confirmed explicitly by the user at the computer (cannot be silently run).
LOCAL_CONFIRM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|[;&|(\s])(sudo|su|doas|runas|gsudo)\b", re.I), "涉及提权命令"),
    (re.compile(r"-verb\s+runas\b", re.I), "涉及 Windows 管理员提权"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(ba|z|da|k)?sh\b", re.I), "涉及下载并直接执行远程脚本"),
    (re.compile(r"\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|\s*(iex|invoke-expression)\b", re.I),
     "涉及下载并直接执行 PowerShell 远程脚本"),
    (re.compile(r"\b(iex|invoke-expression)\b", re.I), "涉及动态代码执行 Invoke-Expression"),
    (re.compile(r"(^|[;&|(\s])eval\b", re.I), "涉及动态求值 eval"),
    (re.compile(r"\breg(\.exe)?\s+(delete|add|import)\b", re.I), "涉及修改 Windows 注册表"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\b", re.I), "涉及危险权限设置 chmod 777"),
]

# Strictly safe read-only commands (deliberately excludes `env` and `printenv` to avoid leaking secrets).
READONLY_CMDS = {
    "ls", "dir", "pwd", "cd", "echo", "printf", "cat", "head", "tail", "wc", "stat", "file",
    "which", "where", "whoami", "hostname", "uname", "date", "uptime",
    "rg", "grep", "egrep", "fgrep", "ag", "fd", "tree", "du", "df", "ps", "lsblk",
    "get-childitem", "get-content", "get-location", "select-string", "test-path",
}
READONLY_GIT_SUBS = {"status", "diff", "log", "show", "branch", "tag", "rev-parse", "ls-files", "remote"}
SHELL_META_WRITE = re.compile(r"(>>?|<<|\$\(|`|;\s*|&&|\|\|)")

REMOVER_CMDS = {"rm", "rmdir", "unlink", "shred", "del", "erase", "rd", "remove-item", "ri"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")"}
DYNAMIC_CHARS = set("$*?[{`")
WIN_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}


class PolicyError(Exception):
    """Hard policy violation (denied outright)."""


@dataclass(frozen=True)
class LocalCheckResult:
    verdict: str         # "allow" | "local_confirm" | "deny"
    cwd: Path | None
    reason: str = ""


def parse_dir_list(text: str) -> list[str]:
    """Split user-entered directories separated by `;` or newlines."""
    return [part.strip().strip('"').strip("'") for part in re.split(r"[;\r\n]+", text or "") if part.strip()]


def normalize_roots(dirs: list[str], *, allow_empty: bool = False) -> list[Path]:
    roots: list[Path] = []
    for d in dirs:
        _validate_raw_path_syntax(d)
        if SENSITIVE_PATH_RE.search(d):
            raise PolicyError(f"禁止授权敏感凭证目录：{d}")
        p = Path(os.path.realpath(os.path.expanduser(d)))
        if SENSITIVE_PATH_RE.search(str(p)):
            raise PolicyError(f"禁止授权敏感凭证目录：{p}")
        if not p.is_dir():
            raise PolicyError(f"允许目录不存在或不是文件夹：{d}")
        if p not in roots:
            roots.append(p)
    if not roots and not allow_empty:
        raise PolicyError("请至少选择一个有效的允许目录")
    return roots


def _is_under(path: Path, root: Path, *, strict: bool = False) -> bool:
    if os.name == "nt":
        p_s = os.path.normcase(os.path.normpath(str(path)))
        r_s = os.path.normcase(os.path.normpath(str(root))).rstrip("\\/")
        if p_s == r_s:
            return not strict
        return p_s.startswith(r_s + os.sep)
    try:
        rel = path.relative_to(root)
        return (rel != Path(".")) if strict else True
    except ValueError:
        return False


def _validate_raw_path_syntax(raw: str) -> None:
    if "\x00" in raw:
        raise PolicyError("路径包含非法空字节")
    stripped = raw.strip()
    # Block UNC network shares (\\host\share or //host/share) — prevents NTLM hash leaks & remote writes.
    if stripped.startswith("\\\\") or stripped.startswith("//"):
        raise PolicyError(f"禁止访问网络 UNC 路径：{raw}")
    # Block Windows drive-relative paths like `C:foo` (missing backslash after colon).
    if re.match(r"^[A-Za-z]:(?![\\/])", stripped):
        raise PolicyError(f"禁止相对盘符路径：{raw}")
    # Block NTFS Alternate Data Streams (`file.txt:stream`) — colon outside leading drive prefix.
    rest = stripped[2:] if re.match(r"^[A-Za-z]:[\\/]", stripped) else stripped
    if ":" in rest:
        raise PolicyError(f"路径包含非法冒号或 NTFS 数据流：{raw}")
    # Block Windows reserved device names in any path segment.
    for part in re.split(r"[\\/]+", rest):
        stem = part.split(".", 1)[0].lower()
        if stem in WIN_RESERVED_NAMES:
            raise PolicyError(f"路径包含系统保留设备名：{raw}")


def resolve_allowed_path(
    raw: str,
    roots: list[Path],
    *,
    must_exist: bool = False,
    base_dir: Path | None = None,
    strict_below_root: bool = False,
) -> Path:
    """Resolve `raw` safely inside `roots`, closing `..` and symlink/junction traversal even on non-existent paths."""
    text = (raw or "").strip()
    if not text:
        if strict_below_root:
            raise PolicyError("目标不能是允许目录根本身")
        return base_dir or roots[0]

    _validate_raw_path_syntax(text)
    if SENSITIVE_PATH_RE.search(text):
        raise PolicyError(f"禁止访问敏感凭证路径：{raw}")

    expanded = os.path.expanduser(text)
    anchor = base_dir or roots[0]
    joined = expanded if os.path.isabs(expanded) else os.path.join(str(anchor), expanded)
    # First lexically normalize all `..` and `.` segments so non-existent suffixes NEVER retain `..`!
    normed = Path(os.path.normpath(joined))

    # Walk up to the deepest existing ancestor to resolve any symlinks/junctions, then re-attach clean tail.
    tail: list[str] = []
    cur = normed
    while not cur.exists() and cur != cur.parent:
        if cur.name in ("", ".", ".."):
            raise PolicyError(f"非法路径段：{raw}")
        tail.append(cur.name)
        cur = cur.parent

    real_base = Path(os.path.realpath(cur))
    resolved = real_base.joinpath(*reversed(tail)) if tail else real_base
    # Final lexical normalization & re-check against realpath if it now exists.
    resolved = Path(os.path.realpath(resolved)) if resolved.exists() else Path(os.path.normpath(str(resolved)))

    if SENSITIVE_PATH_RE.search(str(resolved)):
        raise PolicyError(f"禁止访问敏感凭证路径：{raw}")
    if not any(_is_under(resolved, root, strict=strict_below_root) for root in roots):
        raise PolicyError(f"路径超出本地 --dir 白名单范围：{resolved}")
    if must_exist and not resolved.exists():
        raise PolicyError(f"本地路径不存在：{resolved}")
    return resolved


def _split_shell_segments(command: str) -> list[list[str]] | None:
    """Best-effort split of shell command into simple command token lists; returns None on parse error."""
    # Insert spaces around shell separators outside quotes by scanning with shlex(punctuation_chars=True).
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in SEPARATORS:
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _check_deletions(command: str, safe_cwd: Path, roots: list[Path]) -> tuple[str, str] | None:
    """Analyze destructive delete commands; returns ('deny'|'local_confirm', reason) or None if safe."""
    segments = _split_shell_segments(command)
    if segments is None:
        # Unparseable shell quoting combined with any delete keyword -> require local confirmation.
        if re.search(r"\b(rm|rmdir|unlink|shred|del|erase|remove-item)\b", command, re.I):
            return ("local_confirm", "命令包含删除操作且无法静态解析引号，需本机确认")
        return None

    cur_dir: Path | None = safe_cwd
    for seg in segments:
        # Skip leading env assignments like FOO=1
        i = 0
        while i < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]):
            i += 1
        if i >= len(seg):
            continue
        cmd = os.path.basename(seg[i]).lower()
        if cmd.endswith(".exe"):
            cmd = cmd[:-4]
        args = seg[i + 1:]

        if cmd == "cd":
            target = next((a for a in args if not a.startswith("-")), "")
            if not target or any(ch in target for ch in DYNAMIC_CHARS):
                cur_dir = None
            else:
                try:
                    cur_dir = resolve_allowed_path(target, roots, base_dir=cur_dir or safe_cwd)
                except PolicyError:
                    cur_dir = None
            continue

        if cmd == "xargs" and any(os.path.basename(a).lower().removesuffix(".exe") in REMOVER_CMDS for a in args):
            return ("local_confirm", "xargs 批量删除目标无法静态验证，需本机确认")

        if cmd == "find" and any(a.lower() in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in args):
            # Check find root paths (tokens before first flag starting with `-`)
            find_paths = []
            for a in args:
                if a.startswith("-") or a in ("(", "!", ","):
                    break
                find_paths.append(a)
            find_paths = find_paths or ["."]
            for fp in find_paths:
                if cur_dir is None or any(ch in fp for ch in DYNAMIC_CHARS):
                    return ("local_confirm", f"find 删除目标包含动态路径（{fp}），需本机确认")
                try:
                    resolve_allowed_path(fp, roots, base_dir=cur_dir)
                except PolicyError:
                    return ("deny", f"本地硬拦截：find 删除路径超出 --dir 白名单（{fp}）")
            continue

        if cmd in REMOVER_CMDS:
            targets = [a for a in args if a != "--" and not a.startswith("-") and not a.startswith("/")]
            # Also catch POSIX absolute paths `/...` (which start with `/`)!
            targets = []
            after_dd = False
            for a in args:
                if a == "--" and not after_dd:
                    after_dd = True
                    continue
                if not after_dd and a.startswith("-") and a != "-":
                    continue
                targets.append(a)
            if not targets:
                return ("local_confirm", "删除命令缺少可静态确认的目标路径")
            for target in targets:
                if cur_dir is None:
                    return ("local_confirm", f"前置 cd 目录不明确，删除目标 {target} 需本机确认")
                if any(ch in target for ch in DYNAMIC_CHARS):
                    # If target starts with a safe known prefix under roots before glob (e.g. `build/*.tmp`),
                    # check the static prefix; if empty or root itself, require local confirm / deny.
                    static_part = re.split(r"[$*?\[{`]", target, maxsplit=1)[0]
                    if not static_part or static_part in ("/", "\\", ".", "./"):
                        return ("local_confirm", f"删除目标包含未限定动态/通配路径（{target}），需本机确认")
                    try:
                        resolve_allowed_path(static_part, roots, base_dir=cur_dir, strict_below_root=True)
                    except PolicyError:
                        return ("deny", f"本地硬拦截：删除目标 {target} 可能越出 --dir 白名单")
                    continue
                try:
                    resolve_allowed_path(target, roots, base_dir=cur_dir, strict_below_root=True)
                except PolicyError:
                    return ("deny", f"本地硬拦截：删除目标 {target} 超出 --dir 允许子目录范围")
    return None


def is_readonly_command(command: str, safe_cwd: Path, roots: list[Path]) -> bool:
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
            pass
        elif cmd == "git" and len(tokens) >= 2 and tokens[1].lower() in READONLY_GIT_SUBS:
            if any(t in ("-D", "-d", "--delete", "--force", "-f") for t in tokens[2:]):
                return False
        elif cmd == "find":
            if any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for t in tokens[1:]):
                return False
        else:
            return False
        # Verify any path-like arguments stay inside `roots` (blocks `cat /etc/hosts` or `ls ../..`).
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            looks_like_path = (
                "/" in tok or "\\" in tok or tok.startswith(("~", "."))
                or bool(re.match(r"^[A-Za-z]:", tok))
            )
            if looks_like_path:
                try:
                    resolve_allowed_path(tok, roots, base_dir=safe_cwd)
                except PolicyError:
                    return False
    return True


def evaluate_local_command(
    command: str,
    cwd: str,
    roots: list[Path],
    mode: str,
    *,
    server_approved_id: str | None = None,
    seen_approvals: set[str] | None = None,
    require_local_approval: bool = False,
) -> LocalCheckResult:
    """Comprehensive client-side verification before spawning any local subprocess."""
    try:
        safe_cwd = resolve_allowed_path(cwd, roots, must_exist=True)
        if not safe_cwd.is_dir():
            return LocalCheckResult("deny", None, f"工作目录不是文件夹：{safe_cwd}")
    except PolicyError as exc:
        return LocalCheckResult("deny", None, str(exc))

    if SENSITIVE_PATH_RE.search(command):
        return LocalCheckResult("deny", safe_cwd, "本地硬安全策略拒绝：命令涉及敏感凭证路径")

    for pat, reason in HARD_DENY_PATTERNS:
        if pat.search(command):
            return LocalCheckResult("deny", safe_cwd, f"本地硬熔断禁止执行：{reason}")

    if mode == "readonly":
        if is_readonly_command(command, safe_cwd, roots):
            return LocalCheckResult("allow", safe_cwd, "本地只读策略通过")
        return LocalCheckResult("deny", safe_cwd, "本地策略拒绝：当前绑定为只读模式，仅允许白名单目录内只读命令")

    del_check = _check_deletions(command, safe_cwd, roots)
    if del_check is not None:
        verdict, reason = del_check
        return LocalCheckResult(verdict, safe_cwd, reason)

    for pat, reason in LOCAL_CONFIRM_PATTERNS:
        if pat.search(command):
            return LocalCheckResult("local_confirm", safe_cwd, f"本地高危拦截（{reason}），必须在本机确认")

    # In `ask` mode, verify the command was actually preceded by an approval we witnessed (defense against compromised server).
    if mode == "ask":
        if require_local_approval or not (server_approved_id and seen_approvals and server_approved_id in seen_approvals):
            return LocalCheckResult("local_confirm", safe_cwd, "本机 ask 模式安全复核：未检测到有效审批记录，需本机确认")

    return LocalCheckResult("allow", safe_cwd, "")
