"""API keys of the model providers, kept in one place for every harness, and the subscriptions harnesses log into.

Each provider's key comes either from a command that prints it (e.g. a password
manager, run in the user's login shell like the balance checker's keyCommand) or
from a key saved through the web app, which is written to
<state dir>/keys/<provider>.key (only the owner can read it). Which of the two is
recorded in <state dir>/credentials.json. Keys never go back to the browser and
are not logged; harnesses receive them when they start.

Subscriptions are the harnesses' own logins (`claude auth login`, `codex login`):
they are only detected here, never read or copied.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

KEY_TIMEOUT = 30
KEY_CACHE_SECONDS = 120
MODELS_CACHE_SECONDS = 600
LOGIN_CACHE_SECONDS = 300
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    env: str                 # the environment variable harnesses read the key from


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
    Provider("openai", "OpenAI", "OPENAI_API_KEY"),
    Provider("gemini", "Google Gemini", "GEMINI_API_KEY"),
    Provider("deepseek", "DeepSeek", "DEEPSEEK_API_KEY"),
)}

SUBSCRIPTIONS = {"anthropic": "Claude 订阅", "openai": "ChatGPT 订阅"}


class CredentialError(Exception):
    pass


async def run_key_command(cfg: Config, command: str, env: dict[str, str]) -> str:
    """Run a command that prints a key; the last non-empty line of its output is the key."""
    shell = os.environ.get("SHELL") or "/bin/bash"
    args = [shell, "-lc", command] if cfg.login_shell else ["/bin/sh", "-c", command]
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), KEY_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CredentialError("the key command timed out") from None
    lines = [line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()]
    if proc.returncode != 0 or not lines:
        raise CredentialError(f"the key command failed (exit {proc.returncode})")
    return lines[-1]


def _child_env() -> dict[str, str]:
    from .process import ClaudeProcess
    return ClaudeProcess._child_env()


class Credentials:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.path = cfg.state_dir / "credentials.json"
        self.keys_dir = cfg.state_dir / "keys"
        self._cache: dict[str, tuple[float, str]] = {}
        self._models: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._logins: dict[str, tuple[float, dict[str, Any] | None]] = {}

    # ---- what is configured ------------------------------------------------------------

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._seed()
        except (ValueError, OSError):
            return {}
        providers = data.get("providers") if isinstance(data, dict) else None
        return {k: v for k, v in (providers or {}).items() if k in PROVIDERS and isinstance(v, dict)}

    def _seed(self) -> dict[str, dict[str, Any]]:
        """Before anything was set here: the balance checker's key commands, for the providers it shares."""
        out: dict[str, dict[str, Any]] = {}
        with contextlib.suppress(Exception):
            for p in json.loads(self.cfg.balances_file.read_text(encoding="utf-8")).get("providers", []):
                if p.get("type") in PROVIDERS and p.get("keyCommand"):
                    out[p["type"]] = {"command": str(p["keyCommand"])}
        return out

    def _write(self, providers: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"providers": providers}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _key_file(self, provider: str) -> Path:
        return self.keys_dir / f"{provider}.key"

    def configured(self, provider: str) -> bool:
        entry = self._read().get(provider) or {}
        return bool(entry.get("command")) or (bool(entry.get("stored")) and self._key_file(provider).is_file())

    def status(self) -> list[dict[str, Any]]:
        providers = self._read()
        out = []
        for pid, p in PROVIDERS.items():
            entry = providers.get(pid) or {}
            source = "command" if entry.get("command") else "key" if entry.get("stored") and self._key_file(pid).is_file() else None
            out.append({"id": pid, "name": p.name, "configured": source is not None, "source": source,
                        "command": entry.get("command") if source == "command" else None, "updatedAt": entry.get("updatedAt")})
        return out

    def set_command(self, provider: str, command: str) -> None:
        self._check(provider)
        command = command.strip()
        if not command or "\n" in command:
            raise CredentialError("the command must be one line")
        providers = self._read()
        providers[provider] = {"command": command, "updatedAt": time.time()}
        self._write(providers)
        with contextlib.suppress(FileNotFoundError):
            self._key_file(provider).unlink()
        self._forget(provider)

    def set_key(self, provider: str, key: str) -> None:
        self._check(provider)
        key = key.strip()
        if not key or any(c.isspace() for c in key) or len(key) > 1000:
            raise CredentialError("that does not look like an API key")
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.keys_dir, 0o700)
        path = self._key_file(provider)
        fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        os.replace(path.with_suffix(".tmp"), path)
        providers = self._read()
        providers[provider] = {"stored": True, "updatedAt": time.time()}
        self._write(providers)
        self._forget(provider)

    def clear(self, provider: str) -> None:
        self._check(provider)
        providers = self._read()
        providers.pop(provider, None)
        self._write(providers)
        with contextlib.suppress(FileNotFoundError):
            self._key_file(provider).unlink()
        self._forget(provider)

    def _check(self, provider: str) -> None:
        if provider not in PROVIDERS:
            raise CredentialError(f"unknown provider {provider}")

    def _forget(self, provider: str) -> None:
        self._cache.pop(provider, None)
        self._models.pop(provider, None)

    # ---- the keys themselves ---------------------------------------------------------------

    async def key(self, provider: str) -> str:
        """The provider's key (CredentialError when none is configured or it cannot be had)."""
        self._check(provider)
        cached = self._cache.get(provider)
        if cached and time.time() - cached[0] < KEY_CACHE_SECONDS:
            return cached[1]
        entry = self._read().get(provider) or {}
        if entry.get("command"):
            key = await run_key_command(self.cfg, entry["command"], _child_env())
        elif entry.get("stored") and self._key_file(provider).is_file():
            key = self._key_file(provider).read_text(encoding="utf-8").strip()
        else:
            raise CredentialError(f"no API key is set for {PROVIDERS[provider].name}")
        if not key:
            raise CredentialError(f"the API key for {PROVIDERS[provider].name} is empty")
        self._cache[provider] = (time.time(), key)
        return key

    def helper_command(self, provider: str) -> str | None:
        """A shell command that prints the key, for harnesses that ask for one (Claude Code's apiKeyHelper)."""
        entry = self._read().get(provider) or {}
        if entry.get("command"):
            return str(entry["command"])
        if entry.get("stored") and self._key_file(provider).is_file():
            return f"cat {shlex.quote(str(self._key_file(provider)))}"
        return None

    # ---- what a key reaches --------------------------------------------------------------

    async def models(self, provider: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The provider's chat models, as its API lists them (kept ten minutes)."""
        cached = self._models.get(provider)
        if cached and not refresh and time.time() - cached[0] < MODELS_CACHE_SECONDS:
            return cached[1]
        key = await self.key(provider)
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            models = await LISTERS[provider](session, key)
        self._models[provider] = (time.time(), models)
        return models

    async def test(self, provider: str) -> dict[str, Any]:
        """Whether the key works: lists the provider's models with it."""
        try:
            models = await self.models(provider, refresh=True)
        except CredentialError as exc:
            return {"ok": False, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return {"ok": False, "detail": str(exc) or type(exc).__name__}
        return {"ok": True, "detail": f"{len(models)} 个模型可用", "models": len(models)}

    # ---- subscriptions -----------------------------------------------------------------

    async def subscription(self, provider: str, bins: dict[str, str | None]) -> dict[str, Any] | None:
        """The harness login for a provider's subscription, if there is one: {"plan"}; None otherwise."""
        cached = self._logins.get(provider)
        if cached and time.time() - cached[0] < LOGIN_CACHE_SECONDS:
            return cached[1]
        found: dict[str, Any] | None = None
        try:
            if provider == "anthropic" and bins.get("claude"):
                out = await _output([bins["claude"], "auth", "status"])
                data = json.loads(out)
                if data.get("loggedIn") and data.get("authMethod") == "claude.ai":
                    found = {"plan": data.get("subscriptionType")}
            elif provider == "openai" and bins.get("codex"):
                out = await _output([bins["codex"], "login", "status"])
                if "chatgpt" in out.lower():
                    found = {"plan": None}
        except Exception as exc:  # noqa: BLE001 - no login found
            log.debug("subscription check for %s failed: %s", provider, exc)
        self._logins[provider] = (time.time(), found)
        return found


async def _output(argv: list[str]) -> str:
    env = _child_env()
    env["PATH"] = os.path.dirname(argv[0]) + os.pathsep + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return out.decode(errors="replace")


# ---- model lists -----------------------------------------------------------------------------

async def _get(session: aiohttp.ClientSession, url: str, headers: dict[str, str]) -> Any:
    async with session.get(url, headers={"Accept": "application/json", **headers}) as resp:
        if resp.status in (401, 403):
            raise CredentialError(f"the key was refused (HTTP {resp.status})")
        if resp.status != 200:
            raise CredentialError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


_OPENAI_SKIP = ("audio", "tts", "transcribe", "realtime", "image", "embedding", "moderation", "search", "dall-e", "whisper",
                "davinci", "babbage", "instruct", "sora", "computer-use")


async def _openai_models(session: aiohttp.ClientSession, key: str) -> list[dict[str, Any]]:
    data = await _get(session, "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    ids = [m["id"] for m in data.get("data") or () if isinstance(m.get("id"), str)]
    ids = [i for i in ids if i.startswith(("gpt-", "o")) and not any(s in i for s in _OPENAI_SKIP)
           and not re.search(r"-\d{4}-\d{2}-\d{2}$", i) and not i.startswith("gpt-live")]   # dated snapshots repeat the names
    created = {m["id"]: m.get("created") or 0 for m in data.get("data") or ()}
    ids.sort(key=lambda i: created.get(i, 0), reverse=True)
    return [{"value": i, "displayName": i} for i in ids]


async def _anthropic_models(session: aiohttp.ClientSession, key: str) -> list[dict[str, Any]]:
    data = await _get(session, "https://api.anthropic.com/v1/models?limit=100",
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return [{"value": m["id"], "displayName": m.get("display_name") or m["id"]} for m in data.get("data") or () if m.get("id")]


async def _gemini_models(session: aiohttp.ClientSession, key: str) -> list[dict[str, Any]]:
    data = await _get(session, "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", {"x-goog-api-key": key})
    out = []
    for m in data.get("models") or ():
        name = str(m.get("name") or "").removeprefix("models/")
        if "generateContent" not in (m.get("supportedGenerationMethods") or ()) or not name.startswith("gemini"):
            continue
        if any(s in name for s in ("tts", "image", "embedding", "audio", "live")):
            continue
        out.append({"value": name, "displayName": m.get("displayName") or name, "contextWindow": m.get("inputTokenLimit")})
    return out


async def _deepseek_models(session: aiohttp.ClientSession, key: str) -> list[dict[str, Any]]:
    data = await _get(session, "https://api.deepseek.com/models", {"Authorization": f"Bearer {key}"})
    out = []
    for m in data.get("data") or ():
        if not m.get("id"):
            continue
        levels = (m.get("effort") or {}).get("supported_levels") or []
        out.append({"value": m["id"], "displayName": m.get("name") or m["id"], "supportedEffortLevels": levels or None,
                    "defaultEffort": (m.get("effort") or {}).get("default_level"), "contextWindow": m.get("context_window")})
    return out


LISTERS = {"openai": _openai_models, "anthropic": _anthropic_models, "gemini": _gemini_models, "deepseek": _deepseek_models}
