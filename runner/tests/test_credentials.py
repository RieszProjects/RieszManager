"""The API key store (credentials.py) and how routes set harnesses up (routes.py)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from riesz_runner import routes
from riesz_runner.config import Config
from riesz_runner.credentials import CredentialError, Credentials


def config(tmp: Path) -> Config:
    return Config(workspace_root=tmp, claude_home=tmp / "claude", claude_bin="claude", state_dir=tmp / "state",
                  socket_path=tmp / "runner.sock", token="t" * 32, login_shell=False)


class StoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rz-cred-"))
        self.cfg = config(self.tmp)
        self.creds = Credentials(self.cfg)

    async def test_saved_key_stays_private(self):
        self.creds.set_key("openai", "  sk-test-123  ")
        path = self.tmp / "state" / "keys" / "openai.key"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(await self.creds.key("openai"), "sk-test-123")
        status = {p["id"]: p for p in self.creds.status()}
        self.assertEqual((status["openai"]["configured"], status["openai"]["source"]), (True, "key"))
        self.assertNotIn("sk-test-123", json.dumps(self.creds.status()))
        self.assertNotIn("sk-test-123", (self.tmp / "state" / "credentials.json").read_text())
        self.assertEqual(stat.S_IMODE((self.tmp / "state" / "credentials.json").stat().st_mode), 0o600)

    async def test_key_command(self):
        self.creds.set_command("deepseek", "echo noise; echo sk-from-command")
        self.assertEqual(await self.creds.key("deepseek"), "sk-from-command")
        self.assertEqual(self.creds.helper_command("deepseek"), "echo noise; echo sk-from-command")
        status = {p["id"]: p for p in self.creds.status()}
        self.assertEqual((status["deepseek"]["source"], status["deepseek"]["command"]), ("command", "echo noise; echo sk-from-command"))

    async def test_switching_source_and_clearing(self):
        self.creds.set_key("gemini", "AIza-test")
        self.creds.set_command("gemini", "echo other")
        self.assertFalse((self.tmp / "state" / "keys" / "gemini.key").exists())
        self.assertEqual(await self.creds.key("gemini"), "other")
        self.creds.clear("gemini")
        self.assertFalse(self.creds.configured("gemini"))
        with self.assertRaises(CredentialError):
            await self.creds.key("gemini")

    def test_rejects_bad_input(self):
        with self.assertRaises(CredentialError):
            self.creds.set_key("openai", "two words")
        with self.assertRaises(CredentialError):
            self.creds.set_command("openai", "a\nb")
        with self.assertRaises(CredentialError):
            self.creds.set_key("nobody", "sk")

    async def test_failing_command(self):
        self.creds.set_command("anthropic", "exit 3")
        with self.assertRaises(CredentialError):
            await self.creds.key("anthropic")

    def test_balance_commands_seed_the_store(self):
        self.cfg.state_dir.mkdir(parents=True)
        (self.cfg.state_dir / "balances.json").write_text(json.dumps({"providers": [
            {"type": "deepseek", "keyCommand": "pass deepseek"}, {"type": "novelai", "keyCommand": "pass novelai"}]}))
        status = {p["id"]: p for p in self.creds.status()}
        self.assertEqual(status["deepseek"]["command"], "pass deepseek")
        self.assertFalse(status["openai"]["configured"])

    def test_helper_for_a_saved_key_reads_the_file(self):
        self.creds.set_key("anthropic", "sk-ant-test")
        self.assertEqual(self.creds.helper_command("anthropic"), f"cat {self.tmp}/state/keys/anthropic.key")
        self.assertIsNone(self.creds.helper_command("openai"))


class RoutesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rz-routes-"))
        self.creds = Credentials(config(self.tmp))

    async def describe(self, harness: str, logged_in: set[str]) -> list[dict]:
        async def subscription(provider, bins):
            return {"plan": "max"} if provider in logged_in else None
        with mock.patch.object(self.creds, "subscription", subscription):
            return await routes.describe(harness, self.creds, {})

    async def test_subscription_comes_first(self):
        self.creds.set_command("anthropic", "echo k")
        found = await self.describe("claude", {"anthropic"})
        self.assertEqual([r["id"] for r in found if r["available"]], ["anthropic:subscription", "anthropic:key"])
        self.assertEqual(found[0]["label"], "Claude 订阅")

    async def test_without_a_login_the_key_is_the_default(self):
        self.creds.set_command("deepseek", "echo k")
        found = await self.describe("codex", set())
        self.assertEqual([r["id"] for r in found if r["available"]], ["deepseek:key"])
        self.assertIn("codex login", found[0]["reason"])

    def test_claude_code_on_deepseek(self):
        self.creds.set_command("deepseek", "echo sk-ds")
        setup = routes.claude_setup("deepseek:key", self.creds)
        self.assertEqual(setup.settings, {"apiKeyHelper": "echo sk-ds"})
        env = setup.apply({"ANTHROPIC_API_KEY": "stale", "HOME": "/home/x"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], routes.DEEPSEEK_ANTHROPIC_URL)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("sk-ds", json.dumps(env))          # the key comes through apiKeyHelper, not the environment

    def test_claude_code_subscription_ignores_stray_keys(self):
        env = routes.claude_setup("anthropic:subscription", self.creds).apply({"ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "y"})
        self.assertEqual(env, {})

    def test_claude_code_key_route_needs_a_key(self):
        with self.assertRaises(CredentialError):
            routes.claude_setup("anthropic:key", self.creds)

    def test_codex_providers(self):
        pid, args = routes.codex_provider("openai:subscription")
        self.assertEqual(pid, "openai")
        self.assertEqual(args, routes.codex_provider("deepseek:key")[1])     # all of them, for resuming any thread
        pid, args = routes.codex_provider("deepseek:key")
        self.assertEqual(pid, "rz-deepseek")
        deepseek = next(a for a in args if a.startswith("model_providers.rz-deepseek={ "))
        self.assertIn('env_key = "DEEPSEEK_API_KEY"', deepseek)
        self.assertIn('wire_api = "responses"', deepseek)

    def test_dsh_patch(self):
        self.assertIsNone(routes.dsh_patch("deepseek:key", []))
        text = routes.dsh_patch("gemini:key", [{"value": "gemini-x", "displayName": "Gemini X", "contextWindow": 1000}])
        head, _, config_json = text.partition("config: ")
        self.assertIn("'@deepseek-ai/dsh-llm-pi-ai'", head)
        config_data = json.loads(config_json)
        self.assertEqual(config_data["providers"]["google"],
                         {"apiKeyEnv": "GEMINI_API_KEY", "models": [{"id": "gemini-x", "name": "Gemini X", "contextWindow": 1000}]})
        self.assertEqual(routes.dsh_provider("gemini:key"), "google")


if __name__ == "__main__":
    unittest.main()
