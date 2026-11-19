"""Which model APIs each harness can run on, and how each one is set up — through the harness's own settings only.

A route is a provider plus how the harness signs in to it: "anthropic:subscription"
(Claude Code's own claude.ai login), "deepseek:key" (an API key kept by
credentials.py), ... A harness's routes are listed subscription first, and a
session takes the first one that is available unless told otherwise.

- Claude Code: the claude.ai subscription; an Anthropic API key or DeepSeek's
  Anthropic-compatible API, the key given through Claude Code's `apiKeyHelper`
  setting (so it is not in the environment its commands see).
- Codex: the ChatGPT subscription; an OpenAI API key or DeepSeek's Responses
  API, as custom model providers passed with `-c` (the key in the variable the
  provider names; Codex keeps *KEY* variables out of the commands it runs).
- DeepSeek Harness: DeepSeek directly; OpenAI, Anthropic and Gemini through its
  bundled pi-ai adapter, mounted with a `--patch` file. API keys only: dsh
  could also sign in to subscriptions, but using those outside their own apps
  is against the providers' terms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .credentials import PROVIDERS, SUBSCRIPTIONS, CredentialError, Credentials

ROUTES: dict[str, list[str]] = {
    "claude": ["anthropic:subscription", "anthropic:key", "deepseek:key"],
    "codex": ["openai:subscription", "openai:key", "deepseek:key"],
    "dsh": ["deepseek:key", "openai:key", "anthropic:key", "gemini:key"],
}

DEEPSEEK_ANTHROPIC_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_MODELS = ("deepseek-v4-pro", "deepseek-flash")


def split(route: str) -> tuple[str, str]:
    provider, _, auth = route.partition(":")
    return provider, auth


def label(route: str) -> str:
    provider, auth = split(route)
    if auth == "subscription":
        return SUBSCRIPTIONS.get(provider, f"{provider} 订阅")
    return f"{PROVIDERS[provider].name} API" if provider in PROVIDERS else route


def valid(harness: str, route: str | None) -> bool:
    return bool(route) and route in ROUTES.get(harness, ())


async def describe(harness: str, credentials: Credentials, bins: dict[str, str | None]) -> list[dict[str, Any]]:
    """The harness's routes and whether each can be used now."""
    out = []
    for route in ROUTES.get(harness, ()):
        provider, auth = split(route)
        reason = None
        plan = None
        if auth == "subscription":
            login = await credentials.subscription(provider, bins)
            if login is None:
                reason = "没有登录" + ("（claude auth login）" if provider == "anthropic" else "（codex login）")
            else:
                plan = login.get("plan")
        elif not credentials.configured(provider):
            reason = f"没有设置 {PROVIDERS[provider].name} 的 API key"
        out.append({"id": route, "provider": provider, "auth": auth, "label": label(route), "available": reason is None,
                    "reason": reason, "plan": plan})
    return out


async def default(harness: str, credentials: Credentials, bins: dict[str, str | None]) -> str | None:
    """The first route that can be used: a subscription when the harness is logged in, else a configured key."""
    for r in await describe(harness, credentials, bins):
        if r["available"]:
            return str(r["id"])
    return None


# ---- Claude Code ------------------------------------------------------------------------------

# Variables that would make Claude Code use something other than what the route says.
CLAUDE_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")


@dataclass
class ClaudeSetup:
    """How a Claude Code process is started for a route: variables to set and to remove, settings to add."""
    route: str
    env: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def apply(self, env: dict[str, str]) -> dict[str, str]:
        for name in CLAUDE_AUTH_VARS:
            env.pop(name, None)
        env.update(self.env)
        return env


def claude_setup(route: str, credentials: Credentials) -> ClaudeSetup:
    provider, auth = split(route)
    if route not in ROUTES["claude"]:
        raise CredentialError(f"Claude Code cannot use {route}")
    setup = ClaudeSetup(route)
    if auth == "subscription":
        return setup
    helper = credentials.helper_command(provider)
    if helper is None:
        raise CredentialError(f"no API key is set for {PROVIDERS[provider].name}")
    setup.settings["apiKeyHelper"] = helper
    if provider == "deepseek":
        setup.env.update({
            "ANTHROPIC_BASE_URL": DEEPSEEK_ANTHROPIC_URL,
            # Claude Code's model aliases and its background work (titles, summaries) go to DeepSeek models too.
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
            "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-flash",
        })
    return setup


# ---- Codex ------------------------------------------------------------------------------------

CODEX_PROVIDERS = {
    "openai:key": ("rz-openai", {"name": "OpenAI API", "base_url": "https://api.openai.com/v1", "env_key": "OPENAI_API_KEY",
                                 "wire_api": "responses"}),
    "deepseek:key": ("rz-deepseek", {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "env_key": "DEEPSEEK_API_KEY",
                                     "wire_api": "responses"}),
}


def codex_provider(route: str | None) -> tuple[str, list[str]]:
    """(the thread's modelProvider, extra `codex app-server` arguments) for a route.

    Every custom provider is configured whichever route is used: Codex refuses to
    resume a thread whose provider is not configured, even to move it to another one.
    """
    args: list[str] = []
    for pid, spec in CODEX_PROVIDERS.values():
        table = ", ".join(f"{k} = {json.dumps(v)}" for k, v in spec.items())
        args += ["-c", f"model_providers.{pid}={{ {table} }}"]
    if not route or route == "openai:subscription":
        return "openai", args
    return CODEX_PROVIDERS[route][0], args


# ---- DeepSeek Harness -------------------------------------------------------------------------

DSH_PROVIDERS = {"deepseek": "deepseek-official", "openai": "openai", "anthropic": "anthropic", "gemini": "google"}


def dsh_provider(route: str | None) -> str:
    return DSH_PROVIDERS[split(route or "deepseek:key")[0]]


def dsh_patch(route: str, models: list[dict[str, Any]]) -> str | None:
    """The `--patch` overlay that mounts the route's pi-ai provider (None: DeepSeek, which dsh has built in)."""
    provider = split(route)[0]
    if provider == "deepseek":
        return None
    entries = []
    for m in models:
        entry: dict[str, Any] = {"id": m["value"]}
        if m.get("displayName") and m["displayName"] != m["value"]:
            entry["name"] = m["displayName"]
        if m.get("contextWindow"):
            entry["contextWindow"] = int(m["contextWindow"])
        entries.append(entry)
    config = {"providers": {DSH_PROVIDERS[provider]: {"apiKeyEnv": PROVIDERS[provider].env, "models": entries}}}
    # JSON is YAML: one line per document keeps it simple to write.
    return "- id: llm-pi-ai\n  name: '@deepseek-ai/dsh-llm-pi-ai'\n  config: " + json.dumps(config, ensure_ascii=False) + "\n"
