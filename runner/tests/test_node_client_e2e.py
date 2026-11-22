"""End-to-end integration test: real `riesz-node` client (policy + executor + NodeAgent) <-> real runner server."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT_DIR = ROOT / "client" / "riesz-node"
if str(CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENT_DIR))

from aiohttp.test_utils import TestClient, TestServer

from riesz_node import ed25519_pure as client_ed
from riesz_node.agent import NodeAgent
from riesz_node.policy import PolicyError, check_command_policy, resolve_allowed_path
from riesz_runner.config import Config
from riesz_runner.server import HUB, build_app

try:
    import websockets  # noqa: F401
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


class ClientPolicyTest(unittest.TestCase):
    def test_allowlist_and_symlink_escape_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            allowed = base / "sandbox"
            outside = base / "secret"
            allowed.mkdir()
            outside.mkdir()
            (outside / "data.txt").write_text("secret", encoding="utf-8")
            link = allowed / "escape"
            os.symlink(outside, link)

            ok = resolve_allowed_path("sub/file.txt", [allowed])
            self.assertTrue(str(ok).startswith(str(allowed.resolve())))

            with self.assertRaises(PolicyError):
                resolve_allowed_path("../secret/data.txt", [allowed])
            with self.assertRaises(PolicyError):
                resolve_allowed_path(str(link / "data.txt"), [allowed])
            with self.assertRaises(PolicyError):
                resolve_allowed_path("~/.ssh/id_rsa", [allowed])
            with self.assertRaises(PolicyError):
                check_command_policy("rm -rf foo", str(allowed), [allowed], "readonly")
            # Readonly command passes
            cwd = check_command_policy("ls -la", str(allowed), [allowed], "readonly")
            self.assertEqual(cwd, allowed.resolve())


@unittest.skipUnless(HAS_WEBSOCKETS, "websockets not installed in this venv")
class RealClientServerE2ETest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server_work = root / "server_work"
        self.client_dir = root / "pc_sandbox"
        self.server_work.mkdir()
        self.client_dir.mkdir()
        cfg = Config(
            workspace_root=self.server_work,
            claude_home=root / "claude",
            claude_bin="claude",
            state_dir=root / "state",
            socket_path=root / "runner.sock",
            token="tok-e2e",
        )
        self.app = build_app(cfg)
        # Mount public-style aliases `/api/nodes/*` and `/ws/node` on TestServer to match Django bridge routes
        from riesz_runner import server as srv
        self.app.router.add_get("/ws/node", srv.nodes_ws)
        self.app.router.add_get(r"/api/nodes/transfer/{token:[A-Za-z0-9_-]{16,128}}", srv.nodes_transfer_get)
        self.app.router.add_post(r"/api/nodes/transfer/{token:[A-Za-z0-9_-]{16,128}}", srv.nodes_transfer_post)
        # Bypass runner Bearer check only for those public-style test aliases
        orig_auth = self.app.middlewares[0]
        from aiohttp import web

        @web.middleware
        async def test_auth(request, handler):
            if request.path.startswith(("/ws/node", "/api/nodes/transfer/")):
                return await handler(request)
            return await orig_auth(request, handler)

        self.app.middlewares[0] = test_auth
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer tok-e2e"}
        self.sid = "22222222-3333-4444-8555-666666666666"

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tmp.cleanup()

    async def test_real_agent_connect_bind_exec_push_pull(self):
        hub = self.app[HUB]
        pair = hub.nodes.create_pairing(label="test-laptop", ephemeral=False)
        seed, pub = client_ed.generate_keypair()
        reg = hub.nodes.register_device(
            code=pair["code"], public_key=client_ed.b64u_encode(pub),
            name="test-laptop", os_name="Linux", platform="linux", hostname="laptop",
        )
        server_url = f"http://127.0.0.1:{self.server.port}"
        device = {
            "nodeId": reg["nodeId"], "name": reg["name"],
            "server": server_url, "seed": client_ed.b64u_encode(seed),
            "publicKey": client_ed.b64u_encode(pub), "ephemeral": False,
        }
        agent = NodeAgent(
            device=device, roots=[self.client_dir.resolve()],
            max_mode="full", note="e2e test box", auto_accept_bind=False,
        )
        agent_task = asyncio.create_task(agent.run())
        try:
            for _ in range(100):
                if agent.status == "online":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(agent.status, "online")

            # Request bind from server -> client receives pending_binds -> client approves
            bind_task = asyncio.create_task(hub.nodes.bind_session(reg["nodeId"], self.sid, mode="full"))
            for _ in range(100):
                if agent.pending_binds:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(agent.pending_binds), 1)
            await agent.respond_bind(agent.pending_binds[0], allow=True)
            binding = await bind_task
            self.assertEqual(binding["mode"], "full")

            # Run a real shell command on the local client inside client_dir
            res = await hub.nodes.exec_command(self.sid, command="echo hello_riesz_node && pwd")
            self.assertEqual(res["exitCode"], 0)
            self.assertIn("hello_riesz_node", res["output"])
            self.assertIn(str(self.client_dir.resolve()), res["output"])

            # Real file push (server -> client)
            src_file = self.server_work / "pkg.bin"
            src_file.write_bytes(b"RIESZ-BUILD-ARTIFACT-12345")
            pushed = await hub.nodes.push_file(self.sid, server_path=src_file, dest_path="sub/pkg.bin")
            self.assertEqual((self.client_dir / "sub" / "pkg.bin").read_bytes(), b"RIESZ-BUILD-ARTIFACT-12345")
            self.assertEqual(pushed["size"], len(b"RIESZ-BUILD-ARTIFACT-12345"))

            # Real file pull (client -> server)
            (self.client_dir / "result.txt").write_text("passed-all-checks", encoding="utf-8")
            pulled_dest = self.server_work / "pulled_result.txt"
            pulled = await hub.nodes.pull_file(self.sid, node_path="result.txt", server_dest=pulled_dest)
            self.assertEqual(pulled_dest.read_text(encoding="utf-8"), "passed-all-checks")
            self.assertEqual(pulled["size"], len(b"passed-all-checks"))

            # Client-side unbind blocks further commands immediately
            await agent.unbind_session(self.sid)
            for _ in range(50):
                if not hub.nodes.session_bindings(self.sid):
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(hub.nodes.session_bindings(self.sid), [])
        finally:
            await agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    unittest.main()
