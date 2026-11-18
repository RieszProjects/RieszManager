"""JsonRpcPeer against a scripted child process (tests/fake_rpc_server.py)."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

from riesz_runner.drivers.jsonrpc import JsonRpcError, JsonRpcPeer, PeerClosed

SERVER = str(Path(__file__).parent / "fake_rpc_server.py")


class PeerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.notes = []
        self.asked = []
        self.exited = asyncio.Event()

        async def on_note(method, params):
            self.notes.append((method, params))

        async def on_request(method, params):
            self.asked.append((method, params))
            if params == "refuse":
                raise JsonRpcError(-32099, "refused")
            return {"ok": params}

        async def on_exit(code):
            self.exited.set()

        self.peer = JsonRpcPeer([sys.executable, SERVER], cwd=os.getcwd(), version=True,
                                on_notification=on_note, on_request=on_request, on_exit=on_exit)
        await self.peer.start()

    async def asyncTearDown(self):
        await self.peer.stop()

    async def test_request_and_response(self):
        self.assertEqual(await self.peer.request("echo", {"a": [1, "二"]}), {"a": [1, "二"]})

    async def test_error_response(self):
        with self.assertRaises(JsonRpcError) as ctx:
            await self.peer.request("fail")
        self.assertEqual((ctx.exception.code, ctx.exception.data), (-32001, {"why": "asked to"}))

    async def test_server_asks_the_client(self):
        self.assertEqual(await self.peer.request("ask", "please"), {"ok": "please"})
        self.assertEqual(self.asked, [("confirm", "please")])

    async def test_client_refuses(self):
        answer = await self.peer.request("ask", "refuse")
        self.assertEqual(answer["code"], -32099)

    async def test_notifications(self):
        self.assertEqual(await self.peer.request("notify"), "sent")
        self.assertEqual(self.notes, [("tick", {"n": 1})])

    async def test_long_lines(self):
        self.assertEqual(len(await self.peer.request("big")), 3_000_000)

    async def test_exit_fails_pending_requests(self):
        with self.assertRaises(PeerClosed):
            await self.peer.request("quit")
        await asyncio.wait_for(self.exited.wait(), 5)
        self.assertEqual(self.peer.exit_code, 3)
        with self.assertRaises(PeerClosed):
            await self.peer.request("echo", 1)

    async def test_stop_ends_the_process_group(self):
        child = await self.peer.request("spawn")
        await self.peer.stop(grace=1.0)
        self.assertFalse(self.peer.alive)
        await asyncio.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    async def test_timeout(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.peer.request("nothing-answers-this", timeout=0.3)


if __name__ == "__main__":
    unittest.main()
