"""Ephemeral, session-scoped reverse-connected personal computer nodes (`riesz-node`).

Every client launch is completely independent and stateless on the server:
- A web session generates a short-lived, single-use pairing code bound strictly to THAT session.
- The local computer client creates a fresh in-memory Ed25519 keypair (never written to disk),
  exchanges the pairing code via `POST /api/nodes/register`, and opens `/ws/node` authenticated by
  Ed25519 challenge-response.
- Only the bound session (and its handoff chain) can see or invoke that node.
- Disconnecting immediately destroys the connection and drops all server-side state for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import secrets
import time
import uuid as uuidlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from . import ed25519_pure as ed
from .node_guard import GuardDecision, evaluate_action

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

MODES = ("readonly", "ask", "auto", "full")
MODE_RANK = {m: i for i, m in enumerate(MODES)}
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_PAIR_FAILURES = 10
MAX_TRANSFER_BYTES = 200 * 1024 * 1024
MAX_CMD_OUTPUT_CHARS = 128 * 1024
MAX_CMD_HISTORY = 50
APPROVAL_TIMEOUT = 300.0
PENDING_CONN_TTL = 60.0
TRANSFER_TTL = 180.0


class NodeError(Exception):
    """User-facing error returned to the web app or MCP tool caller."""


def _cap_mode(requested: str, ceiling: str) -> str:
    req = requested if requested in MODE_RANK else "auto"
    ceil = ceiling if ceiling in MODE_RANK else "auto"
    return req if MODE_RANK[req] <= MODE_RANK[ceil] else ceil


def _clean_name(raw: str, fallback: str = "local-pc") -> str:
    name = " ".join(str(raw or "").strip().split())[:48]
    return name or fallback


@dataclass
class PairingCode:
    code: str
    session_id: str
    session_title: str
    harness: str
    project: str
    created_at: float
    expires_at: float
    failures: int = 0


@dataclass
class PendingConnection:
    """Short-lived (60s) single-use ticket created by `register_node` until `/ws/node` handshakes."""
    node_id: str
    public_key: bytes
    session_id: str
    session_title: str
    harness: str
    project: str
    name: str
    os_name: str
    platform: str
    hostname: str
    expires_at: float


@dataclass
class CommandRun:
    id: str
    node_id: str
    session_id: str
    session_title: str
    action: str          # "exec" | "push" | "pull"
    command: str
    cwd: str
    risk: str
    risk_reason: str
    status: str          # "running" | "done" | "error" | "killed" | "denied"
    started_at: float
    ended_at: float | None = None
    exit_code: int | None = None
    output: str = ""
    future: asyncio.Future[dict[str, Any]] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "nodeId": self.node_id,
            "sessionId": self.session_id,
            "sessionTitle": self.session_title,
            "action": self.action,
            "command": self.command,
            "cwd": self.cwd,
            "risk": self.risk,
            "riskReason": self.risk_reason,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "exitCode": self.exit_code,
            "output": self.output[-16384:],
        }


@dataclass
class PendingApproval:
    id: str
    node_id: str
    node_name: str
    session_id: str
    session_title: str
    action: str
    command: str
    cwd: str
    risk: str
    reason: str
    created_at: float
    future: asyncio.Future[bool]

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "nodeId": self.node_id,
            "nodeName": self.node_name,
            "sessionId": self.session_id,
            "sessionTitle": self.session_title,
            "action": self.action,
            "command": self.command,
            "cwd": self.cwd,
            "risk": self.risk,
            "reason": self.reason,
            "createdAt": self.created_at,
        }


@dataclass
class TransferTicket:
    token: str
    transfer_id: str
    node_id: str
    direction: str       # "push" (server -> node) | "pull" (node -> server)
    server_path: Path
    node_path: str
    size: int | None
    sha256: str | None
    expires_at: float
    used: bool = False
    future: asyncio.Future[dict[str, Any]] | None = None


@dataclass
class LiveNode:
    node_id: str
    name: str
    os_name: str
    platform: str
    hostname: str
    session_id: str
    session_title: str
    harness: str
    project: str
    mode: str
    max_mode: str
    dirs: list[str]
    note: str
    connected_at: float
    ws: web.WebSocketResponse
    commands: dict[str, CommandRun] = field(default_factory=dict)
    history: list[CommandRun] = field(default_factory=list)
    _last_chunk_broadcast: float = 0.0

    async def send(self, msg: dict[str, Any]) -> None:
        if self.ws.closed:
            raise NodeError(f"本地节点 {self.name} 已断开连接")
        await self.ws.send_str(json.dumps(msg, ensure_ascii=False))


import json  # noqa: E402


class NodeManager:
    """In-memory manager for ephemeral, session-scoped local computer nodes."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._pairings: dict[str, PairingCode] = {}
        self._pending: dict[str, PendingConnection] = {}
        self._live: dict[str, LiveNode] = {}
        self._approvals: dict[str, PendingApproval] = {}
        self._transfers: dict[str, TransferTicket] = {}
        self._register_failures: list[float] = []

    def _broadcast_soon(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast())
        except RuntimeError:
            pass

    async def broadcast(self) -> None:
        await self.hub.broadcast_global({"ev": "nodes", **self.snapshot()})

    # ---- session chain & unique naming --------------------------------------

    def _handoff_group(self, session_id: str) -> set[str]:
        """All session IDs in the same handoff lineage (walking both `handoffFrom` and `handedOffTo`)."""
        seen: set[str] = {session_id}
        queue = [session_id]
        while queue and len(seen) < 24:
            cur = queue.pop(0)
            s = self.hub.session_summary(cur) or {}
            pref = self.hub.prefs.get(cur) or {}
            for nxt in (s.get("handoffFrom"), s.get("handedOffTo"), pref.get("handedOffTo")):
                if isinstance(nxt, str) and nxt and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen

    def _accessible_owners(self, session_id: str) -> set[str]:
        """Session IDs whose local nodes `session_id` may access:
        - its own handoff lineage;
        - any ancestor session's handoff lineage via `subagentOf` (subagents inherit parent nodes,
          including multi-level nested subagents, while parents/siblings stay isolated).
        """
        owners: set[str] = set()
        queue = [session_id]
        visited: set[str] = set()
        while queue and len(visited) < 32:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            group = self._handoff_group(cur)
            owners.update(group)
            for sid in group:
                pref = self.hub.prefs.get(sid) or {}
                s = self.hub.session_summary(sid) or {}
                parent = pref.get("subagentOf") or s.get("subagentOf")
                if isinstance(parent, str) and parent and parent not in visited:
                    queue.append(parent)
        return owners

    def session_nodes(self, session_id: str) -> list[LiveNode]:
        """All live nodes accessible to `session_id` (own + handoff lineage + inherited from parent sessions)."""
        owners = self._accessible_owners(session_id)
        return [live for live in self._live.values() if live.session_id in owners]

    def _unique_name(self, session_id: str, desired: str, *, exclude_node_id: str | None = None) -> str:
        base = _clean_name(desired)
        used = {
            live.name.lower()
            for live in self.session_nodes(session_id)
            if live.node_id != exclude_node_id
        }
        if base.lower() not in used:
            return base
        for i in range(2, 100):
            cand = f"{base[:42]}-{i}"
            if cand.lower() not in used:
                return cand
        return f"{base[:38]}-{secrets.token_hex(2)}"

    # ---- pairing & ephemeral registration -----------------------------------

    def _prune(self) -> None:
        now = time.time()
        for c, p in list(self._pairings.items()):
            if p.expires_at <= now or p.failures >= MAX_PAIR_FAILURES:
                self._pairings.pop(c, None)
        for nid, pend in list(self._pending.items()):
            if pend.expires_at <= now:
                self._pending.pop(nid, None)

    def create_pairing(self, *, session_id: str, ttl: int = 300) -> dict[str, Any]:
        self._prune()
        if not session_id:
            raise NodeError("生成配对码必须指定会话 ID")
        summary = self.hub.session_summary(session_id) or {}
        title = str(summary.get("title") or summary.get("firstMessage") or session_id[:8])[:80]
        harness = str(summary.get("harness") or self.hub.harnesses.harness_of(session_id) or "claude")
        project = str(summary.get("project") or "")

        ttl = max(60, min(int(ttl), 900))
        part1 = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(4))
        part2 = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(4))
        code = f"RZ-{part1}-{part2}"
        now = time.time()
        entry = PairingCode(
            code=code, session_id=session_id, session_title=title,
            harness=harness, project=project,
            created_at=now, expires_at=now + ttl,
        )
        self._pairings[code] = entry
        self._broadcast_soon()
        return {
            "code": code,
            "sessionId": session_id,
            "sessionTitle": title,
            "createdAt": now,
            "expiresAt": entry.expires_at,
        }

    def register_node(
        self, *, code: str, public_key: str, name: str = "",
        os_name: str = "", platform: str = "", hostname: str = "",
    ) -> dict[str, Any]:
        self._prune()
        now = time.time()
        self._register_failures = [t for t in self._register_failures if now - t < 60.0]
        if len(self._register_failures) >= 20:
            raise NodeError("配对尝试失败次数过多，请 1 分钟后再试")

        norm_code = code.strip().upper()
        pair = self._pairings.get(norm_code)
        if pair is None:
            self._register_failures.append(now)
            for p in self._pairings.values():
                p.failures += 1
            raise NodeError("配对码无效或已过期")

        try:
            pub_bytes = ed.b64u_decode(public_key.strip())
            if len(pub_bytes) != 32:
                raise ValueError("bad length")
        except Exception:
            self._register_failures.append(now)
            raise NodeError("无效的 Ed25519 公钥") from None

        self._pairings.pop(norm_code, None)
        node_id = secrets.token_hex(12)
        default_name = _clean_name(name or hostname or "local-pc")
        assigned_name = self._unique_name(pair.session_id, default_name)

        self._pending[node_id] = PendingConnection(
            node_id=node_id,
            public_key=pub_bytes,
            session_id=pair.session_id,
            session_title=pair.session_title,
            harness=pair.harness,
            project=pair.project,
            name=assigned_name,
            os_name=os_name.strip()[:80],
            platform=platform.strip()[:40],
            hostname=hostname.strip()[:80],
            expires_at=now + PENDING_CONN_TTL,
        )
        self._broadcast_soon()
        return {
            "nodeId": node_id,
            "name": assigned_name,
            "sessionId": pair.session_id,
            "sessionTitle": pair.session_title,
            "harness": pair.harness,
            "project": pair.project,
        }

    # ---- live node actions --------------------------------------------------

    async def kick_node(self, node_id: str) -> None:
        self._pending.pop(node_id, None)
        live = self._live.get(node_id)
        if live is None:
            await self.broadcast()
            return
        with contextlib.suppress(Exception):
            await live.send({"type": "kicked", "reason": "已从网页端断开连接"})
        with contextlib.suppress(Exception):
            await live.ws.close(code=4000, message=b"kicked")

    async def rename_node(self, node_id: str, new_name: str, *, notify_client: bool = True) -> str:
        live = self._live.get(node_id)
        if live is None:
            raise NodeError("该本地节点未在线")
        cleaned = _clean_name(new_name, fallback="")
        if not cleaned:
            raise NodeError("节点名称不能为空")
        final = self._unique_name(live.session_id, cleaned, exclude_node_id=node_id)
        live.name = final
        if notify_client:
            with contextlib.suppress(Exception):
                await live.send({"type": "renamed", "name": final})
        await self.broadcast()
        return final

    # ---- reverse WebSocket handler -----------------------------------------

    async def handle_ws(self, ws: web.WebSocketResponse) -> None:
        self._prune()
        nonce = secrets.token_hex(32)
        ts = int(time.time())
        await ws.send_str(json.dumps({"type": "challenge", "nonce": nonce, "ts": ts}))
        try:
            hello_msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
        except asyncio.TimeoutError:
            await ws.close(code=4408, message=b"handshake timeout")
            return
        if hello_msg.type != WSMsgType.TEXT:
            await ws.close(code=4400, message=b"expected text hello")
            return
        try:
            hello = json.loads(hello_msg.data)
        except ValueError:
            await ws.close(code=4400, message=b"invalid json")
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await ws.close(code=4400, message=b"expected hello")
            return

        node_id = str(hello.get("nodeId") or "").strip()
        sig_b64 = str(hello.get("signature") or "").strip()
        pend = self._pending.pop(node_id, None)
        if pend is None or pend.expires_at <= time.time():
            await ws.send_str(json.dumps({"type": "auth_error", "error": "连接凭证无效或已过期，请重新生成配对码"}))
            await ws.close(code=4403, message=b"unauthorized")
            return

        payload = f"riesz-node-auth:{node_id}:{nonce}:{ts}".encode("utf-8")
        try:
            sig = ed.b64u_decode(sig_b64)
            ok = ed.verify(pend.public_key, payload, sig)
        except Exception:
            ok = False
        if not ok:
            await ws.send_str(json.dumps({"type": "auth_error", "error": "签名校验失败"}))
            await ws.close(code=4403, message=b"bad signature")
            return

        desired_name = str(hello.get("name") or pend.name).strip() or pend.name
        final_name = self._unique_name(pend.session_id, desired_name)
        client_mode = str(hello.get("mode") or hello.get("maxMode") or "auto")
        if client_mode not in MODE_RANK:
            client_mode = "auto"
        raw_dirs = hello.get("dirs")
        dirs = [str(d).strip() for d in raw_dirs if str(d).strip()][:20] if isinstance(raw_dirs, list) else []

        live = LiveNode(
            node_id=node_id,
            name=final_name,
            os_name=str(hello.get("os") or pend.os_name)[:80],
            platform=str(hello.get("platform") or pend.platform)[:40],
            hostname=str(hello.get("hostname") or pend.hostname)[:80],
            session_id=pend.session_id,
            session_title=pend.session_title,
            harness=pend.harness,
            project=pend.project,
            mode=client_mode,
            max_mode=client_mode,
            dirs=dirs,
            note=str(hello.get("note") or "")[:300],
            connected_at=time.time(),
            ws=ws,
        )
        self._live[node_id] = live
        await live.send({
            "type": "welcome",
            "nodeId": node_id,
            "name": live.name,
            "sessionId": live.session_id,
            "sessionTitle": live.session_title,
            "harness": live.harness,
            "project": live.project,
            "mode": live.mode,
        })
        await self.broadcast()
        log.info("local node connected: %s (%s) -> session %s", live.name, node_id, live.session_id[:8])

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        await self._on_message(live, data)
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            await self._on_disconnect(live)

    async def _on_message(self, live: LiveNode, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "ping":
            await live.send({"type": "pong", "t": msg.get("t")})
        elif mtype == "dirs_update":
            if isinstance(msg.get("dirs"), list):
                live.dirs = [str(d).strip() for d in msg["dirs"] if str(d).strip()][:20]
                await self.broadcast()
        elif mtype == "mode_update":
            new_mode = str(msg.get("mode") or "")
            if new_mode in MODE_RANK:
                live.mode = new_mode
                live.max_mode = new_mode
                await self.broadcast()
        elif mtype == "rename":
            raw = str(msg.get("name") or "").strip()
            if raw:
                final = self._unique_name(live.session_id, raw, exclude_node_id=live.node_id)
                live.name = final
                await live.send({"type": "renamed", "name": final})
                await self.broadcast()
        elif mtype == "approval_response":
            aid = str(msg.get("approvalId") or "")
            allow = bool(msg.get("allow"))
            await self.resolve_approval(aid, allow=allow, decided_by="node")
        elif mtype == "exec_chunk":
            cid = str(msg.get("cmdId") or "")
            run = live.commands.get(cid)
            if run is not None:
                chunk = str(msg.get("data") or "")
                if chunk:
                    combined = run.output + chunk
                    if len(combined) > MAX_CMD_OUTPUT_CHARS:
                        combined = combined[-MAX_CMD_OUTPUT_CHARS:]
                    run.output = combined
                    now = time.monotonic()
                    if now - live._last_chunk_broadcast >= 0.4:
                        live._last_chunk_broadcast = now
                        self._broadcast_soon()
        elif mtype == "exec_done":
            cid = str(msg.get("cmdId") or "")
            run = live.commands.pop(cid, None)
            if run is not None:
                run.ended_at = time.time()
                run.exit_code = msg.get("exitCode")
                err = str(msg.get("error") or "")
                if err and not run.output.endswith(err):
                    run.output = (run.output + ("\n" if run.output else "") + err)[-MAX_CMD_OUTPUT_CHARS:]
                if msg.get("killed"):
                    run.status = "killed"
                elif err or (run.exit_code is not None and run.exit_code != 0):
                    run.status = "error"
                else:
                    run.status = "done"
                live.history.append(run)
                if len(live.history) > MAX_CMD_HISTORY:
                    live.history = live.history[-MAX_CMD_HISTORY:]
                if run.future and not run.future.done():
                    run.future.set_result({
                        "exitCode": run.exit_code, "output": run.output,
                        "killed": bool(msg.get("killed")), "error": err or None,
                    })
                await self.broadcast()
        elif mtype == "transfer_done":
            tid = str(msg.get("transferId") or "")
            ticket = next((t for t in self._transfers.values() if t.transfer_id == tid), None)
            if ticket and ticket.future and not ticket.future.done():
                ticket.future.set_result(msg)

    async def _on_disconnect(self, live: LiveNode) -> None:
        if self._live.get(live.node_id) is live:
            self._live.pop(live.node_id, None)
        for run in list(live.commands.values()):
            run.status = "error"
            run.ended_at = time.time()
            if run.future and not run.future.done():
                run.future.set_exception(NodeError(f"本地节点 {live.name} 在执行中途断开连接"))
        live.commands.clear()
        for aid, ap in list(self._approvals.items()):
            if ap.node_id == live.node_id:
                self._approvals.pop(aid, None)
                if not ap.future.done():
                    ap.future.set_result(False)
        for tok, tk in list(self._transfers.items()):
            if tk.node_id == live.node_id:
                self._transfers.pop(tok, None)
                if tk.future and not tk.future.done():
                    tk.future.set_exception(NodeError(f"本地节点 {live.name} 在传输中途断开连接"))
        await self.broadcast()
        log.info("local node disconnected & cleared: %s (%s)", live.name, live.node_id)

    def _resolve_node(self, session_id: str, node_ref: str | None = None) -> LiveNode:
        nodes = self.session_nodes(session_id)
        if not nodes:
            raise NodeError("当前会话未连接任何本地电脑节点。请让用户在本会话中点击「本地电脑」生成配对码并连接。")
        if not node_ref:
            if len(nodes) == 1:
                return nodes[0]
            names = ", ".join(n.name for n in nodes)
            raise NodeError(f"当前会话连接了多个本地节点（{names}），请在 node 参数中指定节点名称。")
        ref = node_ref.strip().lower()
        for n in nodes:
            if n.node_id.lower().startswith(ref) or n.name.lower() == ref:
                return n
        raise NodeError(f"当前会话未连接名为 {node_ref} 的本地节点")

    # ---- approvals ----------------------------------------------------------

    async def _request_approval(
        self, live: LiveNode, *, action: str, command: str, cwd: str, decision: GuardDecision,
    ) -> tuple[bool, str]:
        aid = f"na-{uuidlib.uuid4().hex[:12]}"
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        ap = PendingApproval(
            id=aid, node_id=live.node_id, node_name=live.name,
            session_id=live.session_id, session_title=live.session_title,
            action=action, command=command, cwd=cwd,
            risk=decision.risk, reason=decision.reason, created_at=time.time(),
            future=fut,
        )
        self._approvals[aid] = ap
        await live.send({"type": "approval_request", **ap.summary()})
        await self.broadcast()
        try:
            ok = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
            return ok, aid
        except asyncio.TimeoutError:
            self._approvals.pop(aid, None)
            with contextlib.suppress(Exception):
                await live.send({"type": "approval_resolved", "approvalId": aid, "allow": False, "decidedBy": "timeout"})
            await self.broadcast()
            return False, aid

    async def resolve_approval(self, approval_id: str, *, allow: bool, decided_by: str = "web") -> bool:
        ap = self._approvals.pop(approval_id, None)
        if ap is None:
            return False
        if not ap.future.done():
            ap.future.set_result(bool(allow))
        live = self._live.get(ap.node_id)
        if live is not None:
            with contextlib.suppress(Exception):
                await live.send({"type": "approval_resolved", "approvalId": approval_id, "allow": bool(allow), "decidedBy": decided_by})
        await self.broadcast()
        return True

    # ---- exec & kill --------------------------------------------------------

    async def exec_command(
        self, session_id: str, *, command: str, cwd: str = "", timeout: int = 120, node_ref: str | None = None,
    ) -> dict[str, Any]:
        live = self._resolve_node(session_id, node_ref)
        cmd_text = command.strip()
        if not cmd_text:
            raise NodeError("command 不能为空")
        if not live.dirs:
            raise NodeError(f"本地节点 {live.name} 尚未选择允许目录，请让用户在电脑端界面添加至少一个允许目录")
        timeout = max(5, min(int(timeout), 600))
        eff_cwd = cwd.strip() or live.dirs[0]
        decision = await evaluate_action(
            mode=live.mode, action="exec", command=cmd_text, cwd=eff_cwd,
            allowed_dirs=live.dirs, node_os=live.os_name,
            session_title=live.session_title, credentials=self.hub.credentials,
        )
        if decision.decision == "deny":
            raise NodeError(f"已拒绝在本地节点 {live.name} 执行：{decision.reason}")
        approval_id: str | None = None
        if decision.decision == "ask":
            ok, approval_id = await self._request_approval(live, action="exec", command=cmd_text, cwd=eff_cwd, decision=decision)
            if not ok:
                raise NodeError(f"用户拒绝了在本地节点 {live.name} 执行该命令（{decision.reason}）")

        cmd_id = f"cmd-{uuidlib.uuid4().hex[:10]}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        run = CommandRun(
            id=cmd_id, node_id=live.node_id, session_id=live.session_id,
            session_title=live.session_title, action="exec", command=cmd_text,
            cwd=eff_cwd, risk=decision.risk, risk_reason=decision.reason,
            status="running", started_at=time.time(), future=fut,
        )
        live.commands[cmd_id] = run
        await live.send({
            "type": "exec",
            "cmdId": cmd_id,
            "command": cmd_text,
            "cwd": eff_cwd,
            "timeout": timeout,
            "risk": decision.risk,
            "riskReason": decision.reason,
            **({"approvalId": approval_id} if approval_id else {}),
        })
        await self.broadcast()
        try:
            res = await asyncio.wait_for(fut, timeout=timeout + 20)
        except asyncio.TimeoutError:
            await self.kill_command(live.node_id, cmd_id)
            raise NodeError(f"本地节点命令执行超时（>{timeout}s）") from None
        return {
            "node": live.name, "cmdId": cmd_id, "exitCode": res.get("exitCode"),
            "killed": res.get("killed", False), "error": res.get("error"),
            "output": res.get("output") or "", "risk": decision.risk, "riskReason": decision.reason,
        }

    async def kill_command(self, node_id: str, cmd_id: str) -> bool:
        live = self._live.get(node_id)
        if live is None or cmd_id not in live.commands:
            return False
        with contextlib.suppress(Exception):
            await live.send({"type": "exec_kill", "cmdId": cmd_id})
        return True

    # ---- file push & pull ---------------------------------------------------

    def _prune_transfers(self) -> None:
        now = time.time()
        for tok, tk in list(self._transfers.items()):
            if tk.expires_at <= now and (tk.future is None or tk.future.done()):
                self._transfers.pop(tok, None)

    async def push_file(
        self, session_id: str, *, server_path: Path, dest_path: str, node_ref: str | None = None,
    ) -> dict[str, Any]:
        self._prune_transfers()
        live = self._resolve_node(session_id, node_ref)
        if not live.dirs:
            raise NodeError(f"本地节点 {live.name} 尚未选择允许目录，请让用户在电脑端界面添加至少一个允许目录")
        if not server_path.is_file():
            raise NodeError(f"服务器上找不到文件：{server_path}")
        size = server_path.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            raise NodeError(f"文件超过 200MB 限制（{size} bytes）")
        sha = await asyncio.to_thread(_sha256_file, server_path)
        decision = await evaluate_action(
            mode=live.mode, action="push", command=f"push {server_path.name} ({size}B) -> {dest_path}",
            cwd=live.dirs[0], allowed_dirs=live.dirs, node_os=live.os_name,
            session_title=live.session_title, credentials=self.hub.credentials,
        )
        if decision.decision == "deny":
            raise NodeError(f"已拒绝推送文件到 {live.name}：{decision.reason}")
        if decision.decision == "ask":
            ok, _ = await self._request_approval(
                live, action="push",
                command=f"推送 {server_path.name} ({size}B) -> {dest_path}",
                cwd=dest_path, decision=decision,
            )
            if not ok:
                raise NodeError(f"用户拒绝了向 {live.name} 推送文件")

        token = secrets.token_urlsafe(32)
        tid = f"tr-{uuidlib.uuid4().hex[:10]}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        ticket = TransferTicket(
            token=token, transfer_id=tid, node_id=live.node_id, direction="push",
            server_path=server_path, node_path=dest_path, size=size, sha256=sha,
            expires_at=time.time() + TRANSFER_TTL, future=fut,
        )
        self._transfers[token] = ticket
        await live.send({
            "type": "file_pull_from_server",
            "transferId": tid,
            "url": f"/api/nodes/transfer/{token}",
            "destPath": dest_path,
            "size": size,
            "sha256": sha,
        })
        try:
            res = await asyncio.wait_for(fut, timeout=TRANSFER_TTL)
        except asyncio.TimeoutError:
            self._transfers.pop(token, None)
            raise NodeError("文件推送超时") from None
        finally:
            self._transfers.pop(token, None)
        if not res.get("ok"):
            raise NodeError(str(res.get("error") or "文件推送失败"))
        return {"node": live.name, "destPath": res.get("path") or dest_path, "size": size, "sha256": sha}

    async def pull_file(
        self, session_id: str, *, node_path: str, server_dest: Path, node_ref: str | None = None,
    ) -> dict[str, Any]:
        self._prune_transfers()
        live = self._resolve_node(session_id, node_ref)
        if not live.dirs:
            raise NodeError(f"本地节点 {live.name} 尚未选择允许目录，请让用户在电脑端界面添加至少一个允许目录")
        decision = await evaluate_action(
            mode=live.mode, action="pull", command=f"pull {node_path} -> {server_dest.name}",
            cwd=live.dirs[0], allowed_dirs=live.dirs, node_os=live.os_name,
            session_title=live.session_title, credentials=self.hub.credentials,
        )
        if decision.decision == "deny":
            raise NodeError(f"已拒绝从 {live.name} 拉取文件：{decision.reason}")
        if decision.decision == "ask":
            ok, _ = await self._request_approval(
                live, action="pull",
                command=f"拉取 {node_path} -> {server_dest}",
                cwd=node_path, decision=decision,
            )
            if not ok:
                raise NodeError(f"用户拒绝了从 {live.name} 拉取文件")

        token = secrets.token_urlsafe(32)
        tid = f"tr-{uuidlib.uuid4().hex[:10]}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        ticket = TransferTicket(
            token=token, transfer_id=tid, node_id=live.node_id, direction="pull",
            server_path=server_dest, node_path=node_path, size=None, sha256=None,
            expires_at=time.time() + TRANSFER_TTL, future=fut,
        )
        self._transfers[token] = ticket
        await live.send({
            "type": "file_push_to_server",
            "transferId": tid,
            "url": f"/api/nodes/transfer/{token}",
            "srcPath": node_path,
        })
        try:
            res = await asyncio.wait_for(fut, timeout=TRANSFER_TTL)
        except asyncio.TimeoutError:
            self._transfers.pop(token, None)
            raise NodeError("文件拉取超时") from None
        finally:
            self._transfers.pop(token, None)
        if not res.get("ok"):
            raise NodeError(str(res.get("error") or "文件拉取失败"))
        return {"node": live.name, "serverPath": str(server_dest), "size": ticket.size, "sha256": ticket.sha256}

    def claim_transfer(self, token: str, direction: str) -> TransferTicket:
        self._prune_transfers()
        tk = self._transfers.get(token)
        if tk is None or tk.used or tk.direction != direction or tk.expires_at <= time.time():
            raise NodeError("传输令牌无效或已过期")
        tk.used = True
        return tk

    # ---- snapshot -----------------------------------------------------------

    def snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        self._prune()
        live_list = self.session_nodes(session_id) if session_id else list(self._live.values())
        live_list.sort(key=lambda n: -n.connected_at)
        nodes = [
            {
                "id": n.node_id,
                "name": n.name,
                "os": n.os_name,
                "platform": n.platform,
                "hostname": n.hostname,
                "sessionId": n.session_id,
                "sessionTitle": n.session_title,
                "harness": n.harness,
                "project": n.project,
                "mode": n.mode,
                "maxMode": n.max_mode,
                "dirs": list(n.dirs),
                "note": n.note,
                "connectedAt": n.connected_at,
                "runningCommands": [c.summary() for c in n.commands.values()],
                "recentCommands": [c.summary() for c in n.history[-15:]],
            }
            for n in live_list
        ]
        owners = self._accessible_owners(session_id) if session_id else None
        approvals = [
            a.summary()
            for a in sorted(self._approvals.values(), key=lambda x: x.created_at)
            if owners is None or a.session_id in owners
        ]
        pairings = [
            {
                "code": p.code,
                "sessionId": p.session_id,
                "sessionTitle": p.session_title,
                "createdAt": p.created_at,
                "expiresAt": p.expires_at,
            }
            for p in self._pairings.values()
            if owners is None or p.session_id in owners
        ]
        return {"nodes": nodes, "approvals": approvals, "pairings": pairings}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(256 * 1024):
            h.update(chunk)
    return h.hexdigest()


_ZIP_CACHE: tuple[float, bytes, str] | None = None


def build_client_zip() -> tuple[bytes, str]:
    """Package `client/riesz-node` into an installable zip archive; returns (bytes, sha256_hex)."""
    global _ZIP_CACHE
    client_dir = Path(__file__).resolve().parents[2] / "client" / "riesz-node"
    if not client_dir.is_dir():
        raise NodeError("服务器上未找到 client/riesz-node 目录")
    files: list[Path] = []
    latest_mtime = 0.0
    for p in sorted(client_dir.rglob("*")):
        rel = p.relative_to(client_dir)
        if any(part in ("__pycache__", "tests", ".pytest_cache") or part.endswith(".egg-info") for part in rel.parts):
            continue
        if p.is_file() and not p.name.endswith((".pyc", ".pyo")):
            files.append(p)
            latest_mtime = max(latest_mtime, p.stat().st_mtime)
    if _ZIP_CACHE is not None and _ZIP_CACHE[0] == latest_mtime:
        return _ZIP_CACHE[1], _ZIP_CACHE[2]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            arcname = "riesz-node/" + p.relative_to(client_dir).as_posix()
            zi = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            zf.writestr(zi, p.read_bytes())
    raw = buf.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    _ZIP_CACHE = (latest_mtime, raw, digest)
    return raw, digest


def render_bootstrap_py(origin: str, *, token: str = "") -> str:
    """Self-contained zero-config cross-platform Python launcher for `riesz-node`."""
    safe_origin = origin.rstrip("/")
    _, client_hash = build_client_zip()
    return f'''#!/usr/bin/env python3
"""Zero-config cross-platform launcher for riesz-node (auto-cached venv + auto-update)."""
import hashlib, os, pathlib, subprocess, sys, tempfile, urllib.request

DEFAULT_SERVER = {safe_origin!r}
CLIENT_HASH = {client_hash!r}
DOWNLOAD_TOKEN = {token!r}

def _cache_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home())
        return pathlib.Path(base) / "riesz-node"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".cache") / "riesz-node"

def _venv_bin(venv_dir, name):
    if sys.platform == "win32":
        return venv_dir / "Scripts" / f"{{name}}.exe"
    return venv_dir / "bin" / name

def _download(url, dest):
    headers = {{"User-Agent": "riesz-node/1"}}
    if DOWNLOAD_TOKEN:
        headers["X-Riesz-Download-Token"] = DOWNLOAD_TOKEN
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())

def _main():
    if sys.version_info < (3, 10):
        raise SystemExit("riesz-node 需要 Python 3.10 或更高版本。")
    args = list(sys.argv[1:])
    if args and args[0] == "--":
        args = args[1:]
    server = DEFAULT_SERVER
    for i, a in enumerate(args):
        if a == "--server" and i + 1 < len(args):
            server = args[i + 1].rstrip("/")
        elif a.startswith("--server="):
            server = a.split("=", 1)[1].rstrip("/")
    cache = _cache_dir()
    venv_dir = cache / "venv"
    stamp = cache / "client.sha256"
    exe = _venv_bin(venv_dir, "riesz-node")
    pip = _venv_bin(venv_dir, "pip")
    had_exe = exe.exists()
    if not had_exe:
        print("[riesz-node] 首次运行，正在初始化本地虚拟环境…", flush=True)
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
    need_install = not had_exe or not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != CLIENT_HASH
    if need_install:
        print("[riesz-node] 正在从服务器拉取并安装最新客户端…", flush=True)
        zip_url = f"{{server}}/api/nodes/client.zip"
        with tempfile.TemporaryDirectory() as td:
            zpath = pathlib.Path(td) / "riesz-node.zip"
            _download(zip_url, zpath)
            if server == DEFAULT_SERVER:
                actual = hashlib.sha256(zpath.read_bytes()).hexdigest()
                if actual != CLIENT_HASH:
                    raise SystemExit(f"[riesz-node] 客户端包校验失败（期望 {{CLIENT_HASH[:12]}}，实际 {{actual[:12]}}）")
            if had_exe:
                subprocess.check_call([str(pip), "install", "--quiet", "--no-deps", "--force-reinstall", str(zpath)])
            else:
                subprocess.check_call([str(pip), "install", "--quiet", str(zpath)])
        stamp.write_text(CLIENT_HASH, encoding="utf-8")
    if not args or args[0] != "connect":
        cmd = [str(exe), "connect"]
        if not any(a == "--server" or a.startswith("--server=") for a in args):
            cmd += ["--server", server]
        cmd += args
    else:
        cmd = [str(exe)] + args
    raise SystemExit(subprocess.call(cmd))

_main()
'''
