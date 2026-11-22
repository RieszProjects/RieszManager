"""Three-layer risk evaluation for remote local-node commands (`readonly`, `ask`, `auto`, `full`).

Layer 1: Deterministic hard guard (sensitive paths, privilege escalation, disk/system damage,
         remote-script piping, destructive deletes). Never auto-approved in `auto` mode.
Layer 2: Fast read-only allowlist (zero latency, no LLM call).
Layer 3: Server-side lightweight LLM risk classifier using an already configured API key
         (isolated context, strict JSON, fail-closed -> falls back to `ask` on any error).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .credentials import Credentials

log = logging.getLogger(__name__)

CLASSIFIER_TIMEOUT = 12.0

# Patterns that must NEVER be auto-approved in `auto` mode (escalate to explicit human `ask`).
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|[;&|(\s])(sudo|su|doas|runas|gsudo)\b", re.I), "涉及提权命令"),
    (re.compile(r"-verb\s+runas\b", re.I), "涉及 Windows 管理员提权"),
    (re.compile(r"(^|[;&|(\s])(mkfs(\.\w+)?|fdisk|parted|diskpart|bcdedit|shutdown|reboot|poweroff|halt)\b", re.I), "涉及系统/磁盘级操作"),
    (re.compile(r"(^|[;&|(\s])dd\s+.*\bof\s*=", re.I), "涉及底层写盘命令 dd"),
    (re.compile(r"\breg(\.exe)?\s+(delete|add|import)\b", re.I), "涉及修改 Windows 注册表"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(ba|z|da|k)?sh\b", re.I), "涉及下载并直接管道执行远程脚本"),
    (re.compile(r"\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|\s*(iex|invoke-expression)\b", re.I), "涉及下载并直接执行 PowerShell 远程脚本"),
    (re.compile(r"\b(iex|invoke-expression)\b", re.I), "涉及动态执行代码 Invoke-Expression"),
    (re.compile(r"(^|[;&|(\s])eval\b", re.I), "涉及动态求值 eval"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\b", re.I), "涉及危险权限设置 chmod 777"),
    (re.compile(r"\b(rm|rmdir|del|erase|remove-item)\b.*(\s/\s*$|\s/\*|\b[A-Za-z]:\\(\s|$|\*)|\s~\s*$|\s~/\*|\s\$HOME\b)", re.I), "涉及高危根目录或家目录删除"),
]

_SENSITIVE_PATH_RE = re.compile(
    r"(^|[\s\"'=:/\\])(\.ssh|\.aws|\.gnupg|\.kube|riesz-node|id_rsa|id_ed25519|/etc/shadow|/etc/passwd|"
    r"Login Data|Cookies|Keychain|AppData[/\\]Local[/\\]Google[/\\]Chrome[/\\]User Data|"
    r"\.docker[/\\]config\.json|\.npmrc|\.pypirc|\.netrc)(\b|[/\\]|$)",
    re.I,
)

# Simple read-only commands allowed with zero latency in `readonly` / `auto` mode.
# Deliberately excludes `env`/`printenv` to prevent leaking local environment secrets.
_READONLY_CMDS = {
    "ls", "dir", "pwd", "cd", "echo", "printf", "cat", "head", "tail", "wc", "stat", "file",
    "which", "where", "whoami", "hostname", "uname", "date", "uptime",
    "rg", "grep", "egrep", "fgrep", "ag", "fd", "tree", "du", "df", "ps", "lsblk",
    "get-childitem", "get-content", "get-location", "select-string", "test-path",
}
_READONLY_GIT_SUBS = {"status", "diff", "log", "show", "branch", "tag", "rev-parse", "ls-files", "remote"}
_SHELL_META_WRITE = re.compile(r"(>>?|<<|\$\(|`|;\s*|&&|\|\|)")


@dataclass(frozen=True)
class GuardDecision:
    decision: str          # "allow" | "ask" | "deny"
    risk: str              # "low" | "medium" | "high"
    reason: str
    layer: str             # "hard_guard" | "readonly" | "classifier" | "mode"


def check_hard_guard(command: str) -> GuardDecision | None:
    """Layer 1: deterministic dangerous-command / sensitive-path check."""
    if _SENSITIVE_PATH_RE.search(command):
        return GuardDecision("ask", "high", "命令涉及敏感凭证或系统密钥路径", "hard_guard")
    for pat, reason in _DANGEROUS_PATTERNS:
        if pat.search(command):
            return GuardDecision("ask", "high", reason, "hard_guard")
    return None


def _token_within_allowed(tok: str, cwd: str, allowed_dirs: list[str]) -> bool:
    if not allowed_dirs:
        return True
    if tok.startswith(("~", "\\\\", "//")):
        return False
    # Reject explicit `..` escapes or absolute paths outside allowed_dirs.
    norm_tok = tok.replace("\\", "/")
    is_abs = norm_tok.startswith("/") or bool(re.match(r"^[A-Za-z]:/", norm_tok))
    if not is_abs and ".." not in norm_tok.split("/"):
        return True
    base = (cwd or allowed_dirs[0]).replace("\\", "/").rstrip("/")
    joined = norm_tok if is_abs else f"{base}/{norm_tok}"
    parts: list[str] = []
    prefix = ""
    if re.match(r"^[A-Za-z]:/", joined):
        prefix = joined[:2].lower()
        rest = joined[2:]
    elif joined.startswith("/"):
        prefix = "/"
        rest = joined[1:]
    else:
        rest = joined
    for seg in rest.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            else:
                return False
        else:
            parts.append(seg)
    full = (prefix + "/" + "/".join(parts)) if prefix and prefix != "/" else (prefix + "/".join(parts))
    full_cmp = full.lower() if re.match(r"^[a-z]:/", full) else full
    for root in allowed_dirs:
        r = root.replace("\\", "/").rstrip("/")
        r_cmp = r.lower() if re.match(r"^[a-z]:/", full_cmp) else r
        if full_cmp == r_cmp or full_cmp.startswith(r_cmp + "/"):
            return True
    return False


def is_readonly_command(command: str, allowed_dirs: list[str] | None = None, cwd: str = "") -> bool:
    """Layer 2: conservatively recognise clearly read-only inspection commands inside allowed_dirs."""
    text = command.strip()
    if not text or "\n" in text:
        return False
    if _SENSITIVE_PATH_RE.search(text):
        return False
    # Allow simple single-pipe to readonly filter (e.g. `git log | head -n 20`), reject redirections/subshells/chaining.
    if _SHELL_META_WRITE.search(text):
        return False
    segments = [seg.strip() for seg in text.split("|")]
    if not segments or len(segments) > 3:
        return False
    for seg in segments:
        if not seg:
            return False
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            return False
        if not tokens:
            return False
        cmd = os.path.basename(tokens[0]).lower()
        if cmd.endswith(".exe"):
            cmd = cmd[:-4]
        if cmd in _READONLY_CMDS:
            pass
        elif cmd == "git" and len(tokens) >= 2 and tokens[1].lower() in _READONLY_GIT_SUBS:
            if any(t in ("-D", "-d", "--delete", "--force", "-f") for t in tokens[2:]):
                return False
        elif cmd == "find":
            if any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for t in tokens[1:]):
                return False
        elif cmd in ("python", "python3", "node", "npm", "dotnet", "cargo", "go", "rustc", "gcc", "clang", "cmake", "godot"):
            if not (len(tokens) == 2 and tokens[1] in ("--version", "-v", "-V", "version")):
                return False
        else:
            return False
        if allowed_dirs:
            for tok in tokens[1:]:
                if tok.startswith("-"):
                    continue
                if "/" in tok or "\\" in tok or tok.startswith(("~", ".")) or re.match(r"^[A-Za-z]:", tok):
                    if not _token_within_allowed(tok, cwd, allowed_dirs):
                        return False
    return True


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


_CLASSIFIER_SYSTEM = (
    "You are a strict security risk classifier for a remote computer node controlled by an AI coding agent.\n"
    "Evaluate ONLY the proposed command/action in isolation and decide whether it is safe to run automatically.\n"
    "Respond ONLY with a single JSON object: {\"decision\":\"allow\"|\"ask\"|\"deny\",\"risk\":\"low\"|\"medium\"|\"high\",\"reason\":\"简短中文理由\"}.\n"
    "Rules:\n"
    "- allow (low risk): normal development/testing/inspection/build commands operating inside the allowed directories "
    "(e.g. running unit tests, compilers, local app smoke tests, reading/editing project files inside allowed dirs).\n"
    "- ask (medium/high risk): deleting many files, modifying files outside allowed dirs, installing global packages, "
    "sending data over the network to external hosts, changing system/user settings, accessing credentials, or anything ambiguous.\n"
    "- deny (high risk): obvious malware, credential exfiltration, destructive wipe, privilege escalation.\n"
    "- When in doubt, ALWAYS choose \"ask\"."
)


async def classify_with_llm(
    credentials: Credentials | None,
    *,
    action: str,
    command: str,
    cwd: str,
    allowed_dirs: list[str],
    node_os: str,
    session_title: str,
) -> GuardDecision:
    """Layer 3: call the fastest configured LLM API for isolated risk classification (fail-closed -> ask)."""
    if credentials is None:
        return GuardDecision("ask", "medium", "未配置模型 API，转为人工确认", "classifier")

    preferred = os.environ.get("RUNNER_NODE_CLASSIFIER_PROVIDER", "").strip().lower()
    custom_model = os.environ.get("RUNNER_NODE_CLASSIFIER_MODEL", "").strip()
    order = [preferred] if preferred else []
    for p in ("deepseek", "gemini", "anthropic", "openai"):
        if p not in order:
            order.append(p)

    chosen_provider: str | None = None
    api_key: str | None = None
    for provider in order:
        if not credentials.configured(provider):
            continue
        try:
            api_key = await credentials.key(provider)
            if api_key:
                chosen_provider = provider
                break
        except Exception:  # noqa: BLE001
            continue

    if not chosen_provider or not api_key:
        return GuardDecision("ask", "medium", "无可用 API Key 运行风险分类器，转为人工确认", "classifier")

    user_msg = json.dumps({
        "action": action,
        "command": command[:2000],
        "cwd": cwd,
        "allowed_dirs": allowed_dirs,
        "node_os": node_os,
        "session_title": session_title[:120],
    }, ensure_ascii=False)

    try:
        raw_text = await _call_provider(chosen_provider, api_key, custom_model, user_msg)
        parsed = _extract_json(raw_text)
        if not parsed:
            return GuardDecision("ask", "medium", "风险分类器返回格式无效，转为人工确认", "classifier")
        decision = str(parsed.get("decision") or "ask").lower()
        risk = str(parsed.get("risk") or "medium").lower()
        reason = str(parsed.get("reason") or "模型风险评估").strip()[:160]
        if decision not in ("allow", "ask", "deny"):
            decision = "ask"
        if risk not in ("low", "medium", "high"):
            risk = "medium"
        if decision == "allow" and risk == "high":
            decision = "ask"
        return GuardDecision(decision, risk, reason, "classifier")
    except Exception as exc:  # noqa: BLE001
        log.warning("node risk classifier (%s) failed: %s", chosen_provider, exc)
        return GuardDecision("ask", "medium", f"风险分类器不可用（{type(exc).__name__}），转为人工确认", "classifier")


async def _call_provider(provider: str, api_key: str, custom_model: str, user_msg: str) -> str:
    timeout = aiohttp.ClientTimeout(total=CLASSIFIER_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        if provider == "deepseek":
            model = custom_model or "deepseek-chat"
            async with http.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model, "temperature": 0, "max_tokens": 180,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": _CLASSIFIER_SYSTEM}, {"role": "user", "content": user_msg}],
                },
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    raise RuntimeError(f"deepseek {resp.status}")
                return str(data["choices"][0]["message"]["content"])
        if provider == "openai":
            model = custom_model or "gpt-4.1-mini"
            async with http.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model, "temperature": 0, "max_tokens": 180,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": _CLASSIFIER_SYSTEM}, {"role": "user", "content": user_msg}],
                },
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    raise RuntimeError(f"openai {resp.status}")
                return str(data["choices"][0]["message"]["content"])
        if provider == "anthropic":
            model = custom_model or "claude-haiku-4-5"
            async with http.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                json={
                    "model": model, "max_tokens": 180, "temperature": 0,
                    "system": _CLASSIFIER_SYSTEM,
                    "messages": [{"role": "user", "content": user_msg}],
                },
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    raise RuntimeError(f"anthropic {resp.status}")
                return "".join(b.get("text", "") for b in data.get("content") or () if b.get("type") == "text")
        if provider == "gemini":
            model = custom_model or "gemini-2.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            async with http.post(
                url,
                params={"key": api_key},
                json={
                    "systemInstruction": {"parts": [{"text": _CLASSIFIER_SYSTEM}]},
                    "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 180, "responseMimeType": "application/json"},
                },
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    raise RuntimeError(f"gemini {resp.status}")
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(str(p.get("text") or "") for p in parts)
        raise RuntimeError(f"unsupported provider {provider}")


async def evaluate_action(
    *,
    mode: str,
    action: str,          # "exec" | "push" | "pull"
    command: str,
    cwd: str,
    allowed_dirs: list[str],
    node_os: str,
    session_title: str,
    credentials: Credentials | None,
) -> GuardDecision:
    """Evaluate a local-node action according to the effective permission mode."""
    if action == "pull":
        if _SENSITIVE_PATH_RE.search(command):
            return GuardDecision("deny", "high", "禁止拉取敏感凭证路径", "hard_guard")
        if mode in ("readonly", "auto", "full"):
            return GuardDecision("allow", "low", "允许目录内只读拉取文件", "readonly")
        return GuardDecision("ask", "low", "逐项确认模式：拉取文件需确认", "mode")

    if action == "push":
        if _SENSITIVE_PATH_RE.search(command):
            return GuardDecision("deny", "high", "禁止写入敏感路径", "hard_guard")
        if mode == "readonly":
            return GuardDecision("deny", "medium", "只读模式禁止向本地节点写入文件", "readonly")
        if mode == "full":
            return GuardDecision("allow", "low", "目录内全放行模式", "mode")
        if mode == "ask":
            return GuardDecision("ask", "medium", "逐项确认模式：推送文件需确认", "mode")
        # auto mode: writing inside allowlist is low risk unless classifier flags it
        return await classify_with_llm(
            credentials, action="push_file", command=command, cwd=cwd,
            allowed_dirs=allowed_dirs, node_os=node_os, session_title=session_title,
        )

    # action == "exec"
    hard = check_hard_guard(command)
    if mode == "readonly":
        if hard is None and is_readonly_command(command, allowed_dirs, cwd):
            return GuardDecision("allow", "low", "只读白名单命令", "readonly")
        return GuardDecision("deny", "medium", "只读模式仅允许白名单目录内的查询命令", "readonly")

    if mode == "full":
        if hard is not None:
            return GuardDecision("ask", "high", f"触发安全底线（{hard.reason}），仍需人工确认", "hard_guard")
        return GuardDecision("allow", "low", "目录内全放行模式", "mode")

    if mode == "ask":
        return GuardDecision("ask", hard.risk if hard else "medium",
                             hard.reason if hard else "逐项确认模式", "hard_guard" if hard else "mode")

    # mode == "auto"
    if hard is not None:
        return hard
    if is_readonly_command(command, allowed_dirs, cwd):
        return GuardDecision("allow", "low", "只读命令自动放行", "readonly")
    return await classify_with_llm(
        credentials, action="exec", command=command, cwd=cwd,
        allowed_dirs=allowed_dirs, node_os=node_os, session_title=session_title,
    )
