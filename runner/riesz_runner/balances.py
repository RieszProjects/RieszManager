"""Account balances of other AI services, shown by the doll next to the Claude plan usage.

Which services to check is configured in a local JSON file (RUNNER_BALANCES_FILE,
by default <state dir>/balances.json), which is not part of the repository:

    {"providers": [
        {"type": "deepseek",   "keyCommand": "cat ~/.config/keys/deepseek"},
        {"type": "openrouter", "keyCommand": "printenv OPENROUTER_API_KEY"},
        {"type": "novelai",    "keyCommand": "...", "label": "NovelAI"}
    ]}

`keyCommand` runs in the user's login shell and must print the API key (the last
non-empty line of its output is used). Keys are fetched for each check and never
kept, logged or sent to the browser; only the balances are. Without the file the
doll shows no balances.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .config import Config
from .process import ClaudeProcess

log = logging.getLogger(__name__)

CACHE_SECONDS = 15 * 60
MIN_REFRESH_SECONDS = 60
KEY_TIMEOUT = 30
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)

Balance = dict[str, Any]   # {"amount": float, "unit": "USD" | "CNY" | "Anlas" | a NovelAI tier name}


async def _json(session: aiohttp.ClientSession, url: str, auth: str) -> Any:
    async with session.get(url, headers={"Authorization": auth, "Accept": "application/json"}) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return await resp.json(content_type=None)


async def _deepseek(session: aiohttp.ClientSession, key: str) -> list[Balance]:
    data = await _json(session, "https://api.deepseek.com/user/balance", f"Bearer {key}")
    found = [{"amount": float(b["total_balance"]), "unit": b.get("currency") or "USD"}
             for b in data.get("balance_infos") or [] if b.get("total_balance") is not None]
    # Accounts list one entry per currency; the empty ones are noise. Dollars first.
    found.sort(key=lambda b: b["unit"] != "USD")
    return [b for b in found if b["amount"]] or found[:1]


async def _openrouter(session: aiohttp.ClientSession, key: str) -> list[Balance]:
    data = (await _json(session, "https://openrouter.ai/api/v1/credits", f"Bearer {key}")).get("data") or {}
    return [{"amount": float(data.get("total_credits") or 0) - float(data.get("total_usage") or 0), "unit": "USD"}]


NOVELAI_TIERS = {1: "Tablet", 2: "Scroll", 3: "Opus"}


async def _novelai(session: aiohttp.ClientSession, key: str) -> list[Balance]:
    """The subscription's monthly Anlas (unit: the tier's name, e.g. Opus) and the purchased Anlas."""
    # api.novelai.net refuses third-party clients; the image host serves the same endpoint.
    data = await _json(session, "https://image.novelai.net/user/subscription", f"Bearer {key}")
    steps = data.get("trainingStepsLeft") or {}
    out: list[Balance] = []
    tier = NOVELAI_TIERS.get(data.get("tier"))
    if tier and data.get("active"):
        out.append({"amount": int(steps.get("fixedTrainingStepsLeft") or 0), "unit": tier})
    out.append({"amount": int(steps.get("purchasedTrainingSteps") or 0), "unit": "Anlas"})
    return out


FETCHERS: dict[str, tuple[str, Callable[[aiohttp.ClientSession, str], Awaitable[list[Balance]]]]] = {
    "deepseek": ("DeepSeek", _deepseek),
    "openrouter": ("OpenRouter", _openrouter),
    "novelai": ("NovelAI", _novelai),
}


class BalanceMonitor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._view: dict[str, Any] | None = None
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    @property
    def path(self):
        return self.cfg.balances_file

    def providers(self) -> list[dict[str, Any]]:
        """Read on every check, so edits to the file apply without a restart."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("cannot read %s: %s", self.path, exc)
            return []
        items = data.get("providers") if isinstance(data, dict) else None
        return [p for p in items or [] if isinstance(p, dict) and p.get("type") in FETCHERS and p.get("keyCommand")]

    async def get(self, force: bool = False) -> dict[str, Any]:
        providers = self.providers()
        if not providers:
            return {"providers": [], "fetchedAt": None}
        async with self._lock:
            age = time.time() - self._fetched
            if self._view is not None and (age < MIN_REFRESH_SECONDS or (age < CACHE_SECONDS and not force)):
                return self._view
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                results = await asyncio.gather(*(self._check(session, p) for p in providers))
            self._fetched = time.time()
            self._view = {"providers": results, "fetchedAt": self._fetched}
            return self._view

    async def _check(self, session: aiohttp.ClientSession, p: dict[str, Any]) -> dict[str, Any]:
        kind = p["type"]
        name, fetch = FETCHERS[kind]
        out: dict[str, Any] = {"id": p.get("id") or kind, "label": p.get("label") or name, "balances": [], "error": None}
        try:
            key = await self._key(p["keyCommand"])
            out["balances"] = await fetch(session, key)
        except Exception as exc:  # one failing service must not hide the others
            out["error"] = str(exc) or type(exc).__name__
            log.info("balance check for %s failed: %s", out["label"], out["error"])
        return out

    async def _key(self, command: str) -> str:
        shell = os.environ.get("SHELL") or "/bin/bash"
        args = [shell, "-lc", command] if self.cfg.login_shell else ["/bin/sh", "-c", command]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=ClaudeProcess._child_env(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=KEY_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("key command timed out") from None
        lines = [line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()]
        if proc.returncode != 0 or not lines:
            raise RuntimeError(f"key command failed (exit {proc.returncode})")
        return lines[-1]
