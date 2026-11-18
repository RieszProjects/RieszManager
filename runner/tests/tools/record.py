"""Record the JSON-RPC traffic of real harness sessions, for test fixtures.

    python -m tests.tools.record codex OUT.jsonl WORKDIR
    python -m tests.tools.record dsh OUT.jsonl WORKDIR KEYFILE

Each scenario runs a few short turns in WORKDIR (a scratch folder): create a
file and run commands (approvals are accepted), interrupt a long command,
decline an approval, then read the history back. Every message in either
direction is written to OUT.jsonl as {"t": seconds, "dir": "in"|"out", "msg": {...}}.
The raw recording may contain local paths and prompts; scrub it before it
becomes a fixture (tests/tools/scrub.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from riesz_runner.drivers.jsonrpc import JsonRpcPeer

PROMPT_FILES = "在当前目录创建 hello.txt，内容是 hi。然后分别运行 `cat hello.txt` 和 `ls`。最后用一句话总结你做了什么。"
PROMPT_SLEEP = "运行命令 `sleep 30 && echo done`，等它结束后告诉我。"
PROMPT_DELETE = "删除 hello.txt。"


class Recorder:
    def __init__(self, path: str) -> None:
        self.out = open(path, "w", encoding="utf-8")
        self.t0 = time.time()
        self.notes: list[tuple[str, Any]] = []
        self.events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.decision: dict[str, Any] = {}       # method -> answer for harness requests

    def trace(self, direction: str, message: dict[str, Any]) -> None:
        self.out.write(json.dumps({"t": round(time.time() - self.t0, 3), "dir": direction, "msg": message}, ensure_ascii=False) + "\n")
        self.out.flush()

    def mark(self, label: str) -> None:
        self.out.write(json.dumps({"t": round(time.time() - self.t0, 3), "dir": "mark", "msg": label}, ensure_ascii=False) + "\n")
        self.out.flush()

    async def notification(self, method: str, params: Any) -> None:
        await self.events.put((method, params))

    async def until(self, pred, timeout: float = 240.0) -> tuple[str, Any]:
        deadline = time.time() + timeout
        while True:
            method, params = await asyncio.wait_for(self.events.get(), max(0.1, deadline - time.time()))
            if pred(method, params):
                return method, params


# ---- Codex ---------------------------------------------------------------------------

async def codex(rec: Recorder, workdir: str) -> None:
    approvals: list[str] = []

    async def on_request(method: str, params: Any) -> Any:
        approvals.append(method)
        answer = rec.decision.get(method, {"decision": "accept"})
        return answer

    peer = JsonRpcPeer(["codex", "app-server"], cwd=workdir, version=False, on_notification=rec.notification,
                       on_request=on_request, trace=rec.trace)
    await peer.start()
    try:
        await peer.request("initialize", {"clientInfo": {"name": "riesz-manager", "title": "Riesz Manager", "version": "0.1"},
                                          "capabilities": None})
        await peer.notify("initialized")
        await peer.request("model/list", {})
        started = await peer.request("thread/start", {"cwd": workdir, "approvalPolicy": "untrusted", "sandbox": "workspace-write"})
        thread_id = started["thread"]["id"]

        async def turn(text: str, interrupt_after: float | None = None) -> None:
            res = await peer.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}],
                                                    "effort": "low"})
            turn_id = res["turn"]["id"]
            if interrupt_after is not None:
                await rec.until(lambda m, p: m == "item/started" and (p.get("item") or {}).get("type") == "commandExecution")
                await asyncio.sleep(interrupt_after)
                rec.mark("interrupt")
                await peer.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            await rec.until(lambda m, p: m == "turn/completed" and p.get("threadId") == thread_id)

        rec.mark("turn: files"); await turn(PROMPT_FILES)
        rec.mark("turn: interrupt"); await turn(PROMPT_SLEEP, interrupt_after=3.0)
        rec.decision = {"item/commandExecution/requestApproval": {"decision": "decline"},
                        "item/fileChange/requestApproval": {"decision": "decline"}}
        rec.mark("turn: decline"); await turn(PROMPT_DELETE)
        rec.mark("history")
        await peer.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        await peer.request("thread/turns/list", {"threadId": thread_id, "itemsView": "full"})
        await peer.request("thread/list", {"cwd": workdir, "limit": 5})
        print("codex approvals asked:", approvals, file=sys.stderr)
    finally:
        await peer.stop()


# ---- DeepSeek Harness ------------------------------------------------------------------

async def dsh(rec: Recorder, workdir: str, keyfile: str) -> None:
    env = dict(os.environ, DEEPSEEK_API_KEY=open(keyfile).read().strip())
    approvals: list[Any] = []

    async def on_request(method: str, params: Any) -> Any:
        approvals.append((method, (params or {}).get("toolName")))
        if method == "approval/request":
            return rec.decision.get(method, {"outcome": "allowed-once"})
        if method == "user-question/ask":
            return {"answers": [{"id": q.get("id"), "selected": [], "custom": "随你决定"} for q in (params or {}).get("questions", [])]}
        return {}

    session_id = f"rz-fixture-{int(time.time())}"

    def make_peer() -> JsonRpcPeer:
        return JsonRpcPeer(["dsh", "--profile", "sdk"], cwd=workdir, env=env, version=True,
                           on_notification=rec.notification, on_request=on_request, trace=rec.trace)

    init = {"cwd": workdir, "provider": "deepseek-official", "model": "deepseek-v4-pro", "approvalRequests": True,
            "capabilities": {"userQuestions": True}}

    async def query(peer: JsonRpcPeer, text: str) -> None:
        await peer.request("session/query", {"sessionId": session_id, "contentBlocks": [{"type": "text", "text": text}]})

    async def idle() -> None:
        await rec.until(lambda m, p: m == "session.status" and p.get("sessionId") == session_id and p.get("status") == "idle")

    peer = make_peer()
    await peer.start()
    try:
        await peer.request("initialize", init, timeout=120)
        rec.mark("turn: files"); await query(peer, PROMPT_FILES); await idle()
        rec.mark("turn: interrupt (stop the process, resume)")
        await query(peer, PROMPT_SLEEP)
        await rec.until(lambda m, p: m == "session.event" and ((p.get("event") or {}).get("type") == "tool/call"))
        await asyncio.sleep(3.0)
        await peer.stop()
        peer = make_peer()
        await peer.start()
        await peer.request("initialize", init, timeout=120)
        rec.mark("resume")
        await peer.request("session/resume", {"sessionId": session_id}, timeout=120)
        rec.decision = {"approval/request": {"outcome": "rejected"}}
        rec.mark("turn: decline"); await query(peer, PROMPT_DELETE); await idle()
        rec.decision = {}
        rec.mark("command: /permission read-only")
        await query(peer, "/permission read-only")
        await asyncio.sleep(2.0)
        rec.mark("turn: write while read-only"); await query(peer, "把 hi again 追加到 hello.txt 末尾。"); await idle()
        await peer.request("shutdown", {}, timeout=30)
        print("dsh approvals asked:", approvals, file=sys.stderr)
        print("dsh session:", session_id, file=sys.stderr)
    finally:
        await peer.stop()


def main() -> None:
    which, out, workdir = sys.argv[1], sys.argv[2], os.path.abspath(sys.argv[3])
    os.makedirs(workdir, exist_ok=True)
    rec = Recorder(out)
    if which == "codex":
        asyncio.run(codex(rec, workdir))
    elif which == "dsh":
        asyncio.run(dsh(rec, workdir, sys.argv[4]))
    else:
        raise SystemExit("codex or dsh")


if __name__ == "__main__":
    main()
