"""Reverse-connected personal computer nodes (`riesz-node`).

A personal computer initiates an outbound WSS connection (`/ws/node` -> `/v1/nodes/ws`),
authenticates via Ed25519 challenge-response (paired through a short-lived one-time code),
stays in standby until explicitly bound to a specific session (with computer-side consent),
and exposes controlled command execution + streaming file transfer to that session's MCP tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
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
BIND_TIMEOUT = 120.0
TRANSFER_TTL = 180.0


class NodeError(Exception):
    """User-facing error returned to the web app or MCP tool caller."""


def _cap_mode(requested: str, ceiling: str) -> str:
    req = requested if requested in MODE_RANK else "auto"
    ceil = ceiling if ceiling in MODE_RANK else "auto"
    return req if MODE_RANK[req] <= MODE_RANK[ceil] else ceil


@dataclass
class PairingCode:
    code: str
    label: str
    ephemeral: bool
    created_at: float
    expires_at: float
    failures: int = 0


@dataclass
class SessionBinding:
    session_id: str
    session_title: str
    harness: str
    project: str
    mode: str
    bound_at: float
    dirs: list[str] = field(default_factory=list)


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

    def summary(self, *, include_output: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
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
        }
        if include_output:
            out["output"] = self.output[-8000:]
        return out


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
    direction: str       # "push" (server -> node, GET) | "pull" (node -> server, POST)
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
    ws: web.WebSocketResponse
    name: str
    os_name: str
    platform: str
    hostname: str
    dirs: list[str]
    max_mode: str
    note: str
    version: str
    connected_at: float
    bindings: dict[str, SessionBinding] = field(default_factory=dict)
    pending_binds: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    commands: dict[str, CommandRun] = field(default_factory=dict)
    history: list[CommandRun] = field(default_factory=list)
    _last_chunk_broadcast: float = 0.0

    async def send(self, msg: dict[str, Any]) -> None:
        if self.ws.closed:
            raise NodeError(f"本地节点 {self.name} 已断开连接")
        await self.ws.send_str(json.dumps(msg, ensure_ascii=False))


class NodeManager:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.path = hub.cfg.state_dir / "nodes.json"
        self._devices: dict[str, dict[str, Any]] = {}
        self._ephemeral_devices: dict[str, dict[str, Any]] = {}
        self._pairings: dict[str, PairingCode] = {}
        self._live: dict[str, LiveNode] = {}
        self._approvals: dict[str, PendingApproval] = {}
        self._transfers: dict[str, TransferTicket] = {}
        self._register_failures: list[float] = []
        self._load()

    # ---- persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (ValueError, OSError):
            log.warning("could not read %s", self.path)
            return
        devices = data.get("devices") if isinstance(data, dict) else None
        if isinstance(devices, dict):
            self._devices = {k: v for k, v in devices.items() if isinstance(v, dict)}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": self._devices}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _device(self, node_id: str) -> dict[str, Any] | None:
        return self._devices.get(node_id) or self._ephemeral_devices.get(node_id)

    def _broadcast_soon(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast())
        except RuntimeError:
            pass

    async def broadcast(self) -> None:
        await self.hub.broadcast_global({"ev": "nodes", **self.snapshot()})

    # ---- pairing & device lifecycle ----------------------------------------

    def _prune_pairings(self) -> None:
        now = time.time()
        expired = [c for c, p in self._pairings.items() if p.expires_at <= now or p.failures >= MAX_PAIR_FAILURES]
        for c in expired:
            self._pairings.pop(c, None)

    def create_pairing(self, *, label: str = "", ephemeral: bool = False, ttl: int = 300) -> dict[str, Any]:
        self._prune_pairings()
        ttl = max(60, min(int(ttl), 900))
        part1 = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(4))
        part2 = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(4))
        code = f"RZ-{part1}-{part2}"
        now = time.time()
        pair = PairingCode(code=code, label=label.strip()[:60], ephemeral=bool(ephemeral),
                           created_at=now, expires_at=now + ttl)
        self._pairings[code] = pair
        self._broadcast_soon()
        return {"code": code, "label": pair.label, "ephemeral": pair.ephemeral,
                "createdAt": pair.created_at, "expiresAt": pair.expires_at}

    def register_device(self, *, code: str, public_key: str, name: str = "", os_name: str = "",
                        platform: str = "", hostname: str = "") -> dict[str, Any]:
        """Consume a one-time pairing code and register the client's Ed25519 public key."""
        now = time.time()
        self._register_failures = [t for t in self._register_failures if now - t < 60.0]
        if len(self._register_failures) >= 20:
            raise NodeError("配对失败次数过多，请 1 分钟后再试")
        self._prune_pairings()
        norm_code = code.strip().upper()
        pair = self._pairings.get(norm_code)
        if pair is None:
            self._register_failures.append(now)
            for p in self._pairings.values():
                p.failures += 1
            self._prune_pairings()
            raise NodeError("配对码无效或已过期")
        try:
            pub_raw = ed.b64u_decode(public_key.strip())
            if len(pub_raw) != 32:
                raise ValueError("bad length")
            ed._decode_point(pub_raw)
        except Exception as exc:
            raise NodeError("无效的 Ed25519 公钥") from exc
        # Consume the one-time pairing code immediately.
        self._pairings.pop(norm_code, None)
        node_id = uuidlib.uuid4().hex[:16]
        now = time.time()
        clean_name = (name or pair.label or hostname or f"node-{node_id[:6]}").strip()[:60]
        rec = {
            "id": node_id,
            "name": clean_name,
            "publicKey": ed.b64u_encode(pub_raw),
            "os": os_name.strip()[:80],
            "platform": platform.strip()[:32],
            "hostname": hostname.strip()[:80],
            "ephemeral": pair.ephemeral,
            "createdAt": now,
            "lastSeen": now,
            "revoked": False,
        }
        if pair.ephemeral:
            self._ephemeral_devices[node_id] = rec
        else:
            self._devices[node_id] = rec
            self._save()
        self._broadcast_soon()
        return {"nodeId": node_id, "name": clean_name, "ephemeral": pair.ephemeral}

    async def kick_device(self, node_id: str) -> None:
        live = self._live.get(node_id)
        if live is None:
            return
        with contextlib.suppress(Exception):
            await live.send({"type": "kicked", "reason": "由网页端断开连接"})
        with contextlib.suppress(Exception):
            await live.ws.close(code=4000, message=b"kicked")

    async def revoke_device(self, node_id: str) -> None:
        rec = self._device(node_id)
        if rec is None:
            raise NodeError("未找到该节点")
        rec["revoked"] = True
        if node_id in self._devices:
            self._save()
        await self.kick_device(node_id)
        await self.broadcast()

    async def delete_device(self, node_id: str) -> None:
        await self.kick_device(node_id)
        removed = self._devices.pop(node_id, None) or self._ephemeral_devices.pop(node_id, None)
        if removed is None:
            raise NodeError("未找到该节点")
        if node_id not in self._ephemeral_devices:
            self._save()
        await self.broadcast()

    # ---- reverse WebSocket handler -----------------------------------------

    async def handle_ws(self, ws: web.WebSocketResponse) -> None:
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
        rec = self._device(node_id)
        if rec is None or rec.get("revoked"):
            await ws.send_str(json.dumps({"type": "auth_error", "error": "设备未注册或已被吊销"}))
            await ws.close(code=4401, message=b"unauthorized")
            return
        try:
            pub = ed.b64u_decode(str(rec["publicKey"]))
            sig = ed.b64u_decode(sig_b64)
        except Exception:
            await ws.send_str(json.dumps({"type": "auth_error", "error": "签名格式无效"}))
            await ws.close(code=4401, message=b"bad signature")
            return
        payload = f"riesz-node-auth:{node_id}:{nonce}:{ts}".encode("utf-8")
        if not ed.verify(pub, payload, sig):
            await ws.send_str(json.dumps({"type": "auth_error", "error": "Ed25519 身份验签失败"}))
            await ws.close(code=4401, message=b"invalid signature")
            return

        # Replace any stale connection for the same device key.
        old = self._live.get(node_id)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.ws.close(code=4001, message=b"replaced by new connection")

        dirs = [str(d).strip() for d in (hello.get("dirs") or ()) if str(d).strip()][:20]
        max_mode = str(hello.get("maxMode") or "auto")
        if max_mode not in MODE_RANK:
            max_mode = "auto"
        name = str(hello.get("name") or rec["name"]).strip()[:60] or rec["name"]
        os_name = str(hello.get("os") or rec.get("os") or "").strip()[:80]
        platform = str(hello.get("platform") or rec.get("platform") or "").strip()[:32]
        hostname = str(hello.get("hostname") or rec.get("hostname") or "").strip()[:80]
        note = str(hello.get("note") or "").strip()[:240]
        version = str(hello.get("version") or "1").strip()[:24]

        now = time.time()
        rec.update({"name": name, "os": os_name, "platform": platform, "hostname": hostname, "lastSeen": now})
        if node_id in self._devices:
            self._save()

        live = LiveNode(
            node_id=node_id, ws=ws, name=name, os_name=os_name, platform=platform,
            hostname=hostname, dirs=dirs, max_mode=max_mode, note=note, version=version,
            connected_at=now,
        )
        self._live[node_id] = live
        await live.send({"type": "welcome", "nodeId": node_id, "name": name, "serverTime": now})
        log.info("local node connected: %s (%s, %s, max=%s)", name, node_id, os_name, max_mode)
        await self.broadcast()

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
        elif mtype == "bind_response":
            bid = str(msg.get("bindId") or "")
            fut = live.pending_binds.pop(bid, None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif mtype == "bind_update":
            sid = str(msg.get("sessionId") or "")
            b = live.bindings.get(sid)
            if b is not None and isinstance(msg.get("dirs"), list):
                b.dirs = [str(d).strip() for d in msg["dirs"] if str(d).strip()][:20]
                await self.broadcast()
        elif mtype == "roots_update":
            if isinstance(msg.get("dirs"), list):
                live.dirs = [str(d).strip() for d in msg["dirs"] if str(d).strip()][:20]
                await self.broadcast()
        elif mtype == "unbind":
            sid = str(msg.get("sessionId") or "")
            if sid and live.bindings.pop(sid, None) is not None:
                self._cancel_session_work(live, sid, "电脑端主动解除了会话绑定")
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
        rec = self._device(live.node_id)
        if rec is not None:
            rec["lastSeen"] = time.time()
            if live.node_id in self._devices:
                self._save()
            elif rec.get("ephemeral"):
                self._ephemeral_devices.pop(live.node_id, None)
        for fut in list(live.pending_binds.values()):
            if not fut.done():
                fut.set_exception(NodeError(f"本地节点 {live.name} 已断开连接"))
        live.pending_binds.clear()
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
                    tk.future.set_exception(NodeError(f"本地节点 {live.name} 在文件传输中途断开连接"))
        live.bindings.clear()
        log.info("local node disconnected: %s (%s)", live.name, live.node_id)
        await self.broadcast()

    def _cancel_session_work(self, live: LiveNode, session_id: str, reason: str) -> None:
        for aid, ap in list(self._approvals.items()):
            if ap.node_id == live.node_id and ap.session_id == session_id:
                self._approvals.pop(aid, None)
                if not ap.future.done():
                    ap.future.set_result(False)
        for cid, run in list(live.commands.items()):
            if run.session_id == session_id:
                run.status = "killed"
                run.ended_at = time.time()
                live.commands.pop(cid, None)
                if run.future and not run.future.done():
                    run.future.set_exception(NodeError(reason))

    # ---- session binding ----------------------------------------------------

    async def bind_session(self, node_id: str, session_id: str, *, mode: str = "auto") -> dict[str, Any]:
        live = self._live.get(node_id)
        if live is None:
            raise NodeError("该本地节点当前未在线")
        eff_mode = _cap_mode(mode, live.max_mode)
        summary = self.hub.session_summary(session_id) or {}
        title = str(summary.get("customTitle") or summary.get("title") or summary.get("firstPrompt") or session_id[:8])[:80]
        harness = str(summary.get("harness") or "claude")
        project = str(summary.get("project") or "")

        bind_id = uuidlib.uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        live.pending_binds[bind_id] = fut
        await live.send({
            "type": "bind_request",
            "bindId": bind_id,
            "sessionId": session_id,
            "sessionTitle": title,
            "harness": harness,
            "project": project,
            "mode": eff_mode,
        })
        try:
            resp = await asyncio.wait_for(fut, timeout=BIND_TIMEOUT)
        except asyncio.TimeoutError:
            live.pending_binds.pop(bind_id, None)
            raise NodeError("等待电脑端确认绑定超时") from None
        if not resp.get("allow"):
            reason = str(resp.get("reason") or "电脑端拒绝了绑定请求")
            raise NodeError(reason)
        agreed = _cap_mode(str(resp.get("mode") or eff_mode), live.max_mode)
        resp_dirs = resp.get("dirs")
        bound_dirs = [str(d).strip() for d in resp_dirs if str(d).strip()][:20] if isinstance(resp_dirs, list) else list(live.dirs)
        binding = SessionBinding(
            session_id=session_id, session_title=title, harness=harness,
            project=project, mode=agreed, bound_at=time.time(), dirs=bound_dirs,
        )
        live.bindings[session_id] = binding
        await self.broadcast()
        return {
            "nodeId": node_id, "nodeName": live.name, "sessionId": session_id,
            "mode": agreed, "dirs": bound_dirs, "boundAt": binding.bound_at,
        }

    async def unbind_session(self, node_id: str, session_id: str) -> None:
        live = self._live.get(node_id)
        if live is None:
            return
        live.bindings.pop(session_id, None)
        self._cancel_session_work(live, session_id, "会话已解除本地节点绑定")
        with contextlib.suppress(Exception):
            await live.send({"type": "unbind", "sessionId": session_id})
        await self.broadcast()

    def _chain(self, session_id: str) -> list[str]:
        seen = [session_id]
        cur = session_id
        for _ in range(12):
            s = self.hub.session_summary(cur) or {}
            prev = s.get("handoffFrom")
            if not prev or prev in seen:
                break
            seen.append(str(prev))
            cur = str(prev)
        return seen

    def session_bindings(self, session_id: str) -> list[tuple[LiveNode, SessionBinding]]:
        """All online nodes currently bound to `session_id` (also checking its handoff chain)."""
        chain = self._chain(session_id)
        out: list[tuple[LiveNode, SessionBinding]] = []
        for live in self._live.values():
            for sid in chain:
                if sid in live.bindings:
                    out.append((live, live.bindings[sid]))
                    break
        return out

    def _resolve_bound(self, session_id: str, node_ref: str | None) -> tuple[LiveNode, SessionBinding]:
        bound = self.session_bindings(session_id)
        if not bound:
            raise NodeError("当前会话未绑定任何在线的本地电脑节点。请让用户在 Riesz Manager 页面先将本机绑定到当前会话。")
        if not node_ref:
            if len(bound) == 1:
                return bound[0]
            names = "、".join(f"{n.name} ({n.node_id[:6]})" for n, _ in bound)
            raise NodeError(f"当前会话绑定了多个本地节点，请用 node 参数指定：{names}")
        ref = node_ref.strip().lower()
        for live, b in bound:
            if live.node_id.lower().startswith(ref) or live.name.lower() == ref:
                return live, b
        raise NodeError(f"当前会话未绑定名为 {node_ref} 的本地节点")

    # ---- approvals ----------------------------------------------------------

    async def _request_approval(
        self, live: LiveNode, binding: SessionBinding, *,
        action: str, command: str, cwd: str, decision: GuardDecision,
    ) -> tuple[bool, str]:
        aid = f"na-{uuidlib.uuid4().hex[:12]}"
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        ap = PendingApproval(
            id=aid, node_id=live.node_id, node_name=live.name,
            session_id=binding.session_id, session_title=binding.session_title,
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
        live, binding = self._resolve_bound(session_id, node_ref)
        cmd_text = command.strip()
        if not cmd_text:
            raise NodeError("command 不能为空")
        timeout = max(5, min(int(timeout), 600))
        allowed_dirs = binding.dirs or live.dirs
        eff_cwd = cwd.strip() or (allowed_dirs[0] if allowed_dirs else "")
        decision = await evaluate_action(
            mode=binding.mode, action="exec", command=cmd_text, cwd=eff_cwd,
            allowed_dirs=allowed_dirs, node_os=live.os_name,
            session_title=binding.session_title, credentials=self.hub.credentials,
        )
        if decision.decision == "deny":
            raise NodeError(f"已拒绝在本地节点 {live.name} 执行：{decision.reason}")
        approval_id: str | None = None
        if decision.decision == "ask":
            ok, approval_id = await self._request_approval(live, binding, action="exec", command=cmd_text, cwd=eff_cwd, decision=decision)
            if not ok:
                raise NodeError(f"用户拒绝了在本地节点 {live.name} 执行该命令（{decision.reason}）")

        cmd_id = f"cmd-{uuidlib.uuid4().hex[:10]}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        run = CommandRun(
            id=cmd_id, node_id=live.node_id, session_id=binding.session_id,
            session_title=binding.session_title, action="exec", command=cmd_text,
            cwd=eff_cwd, risk=decision.risk, risk_reason=decision.reason,
            status="running", started_at=time.time(), future=fut,
        )
        live.commands[cmd_id] = run
        await live.send({
            "type": "exec",
            "cmdId": cmd_id,
            "sessionId": binding.session_id,
            "sessionTitle": binding.session_title,
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
        live, binding = self._resolve_bound(session_id, node_ref)
        if not server_path.is_file():
            raise NodeError(f"服务器上找不到文件：{server_path}")
        size = server_path.stat().st_size
        if size > MAX_TRANSFER_BYTES:
            raise NodeError(f"文件超过 200MB 限制（{size} bytes）")
        sha = await asyncio.to_thread(_sha256_file, server_path)
        allowed_dirs = binding.dirs or live.dirs
        decision = await evaluate_action(
            mode=binding.mode, action="push", command=f"push {server_path.name} ({size}B) -> {dest_path}",
            cwd=allowed_dirs[0] if allowed_dirs else "", allowed_dirs=allowed_dirs, node_os=live.os_name,
            session_title=binding.session_title, credentials=self.hub.credentials,
        )
        if decision.decision == "deny":
            raise NodeError(f"已拒绝推送文件到 {live.name}：{decision.reason}")
        if decision.decision == "ask":
            ok, _ = await self._request_approval(live, binding, action="push",
                                                 command=f"推送 {server_path.name} ({size}B) -> {dest_path}",
                                                 cwd=dest_path, decision=decision)
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
            "sessionId": binding.session_id,
            "sessionTitle": binding.session_title,
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
        live, binding = self._resolve_bound(session_id, node_ref)
        allowed_dirs = binding.dirs or live.dirs
        decision = await evaluate_action(
            mode=binding.mode, action="pull", command=f"pull {node_path} -> {server_dest.name}",
            cwd=allowed_dirs[0] if allowed_dirs else "", allowed_dirs=allowed_dirs, node_os=live.os_name,
            session_title=binding.session_title, credentials=self.hub.credentials,
        )
        if decision.decision == "deny":
            raise NodeError(f"已拒绝从 {live.name} 拉取文件：{decision.reason}")
        if decision.decision == "ask":
            ok, _ = await self._request_approval(live, binding, action="pull",
                                                 command=f"拉取 {node_path} -> {server_dest}",
                                                 cwd=node_path, decision=decision)
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
            "sessionId": binding.session_id,
            "sessionTitle": binding.session_title,
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

    def snapshot(self) -> dict[str, Any]:
        self._prune_pairings()
        devices = []
        all_ids = list(self._devices) + [k for k in self._ephemeral_devices if k not in self._devices]
        for nid in all_ids:
            rec = self._device(nid)
            if rec is None:
                continue
            live = self._live.get(nid)
            devices.append({
                "id": nid,
                "name": live.name if live else rec["name"],
                "os": live.os_name if live else rec.get("os", ""),
                "platform": live.platform if live else rec.get("platform", ""),
                "hostname": live.hostname if live else rec.get("hostname", ""),
                "ephemeral": bool(rec.get("ephemeral")),
                "revoked": bool(rec.get("revoked")),
                "createdAt": rec.get("createdAt"),
                "lastSeen": rec.get("lastSeen"),
                "online": live is not None,
                "connectedAt": live.connected_at if live else None,
                "dirs": live.dirs if live else [],
                "maxMode": live.max_mode if live else None,
                "note": live.note if live else "",
                "bindings": [
                    {
                        "sessionId": b.session_id, "sessionTitle": b.session_title,
                        "harness": b.harness, "project": b.project,
                        "mode": b.mode, "dirs": b.dirs or live.dirs, "boundAt": b.bound_at,
                    }
                    for b in (live.bindings.values() if live else ())
                ],
                "runningCommands": [c.summary() for c in (live.commands.values() if live else ())],
                "recentCommands": [c.summary() for c in (live.history[-15:] if live else ())],
            })
        devices.sort(key=lambda d: (not d["online"], -(d["connectedAt"] or d["lastSeen"] or 0)))
        return {
            "devices": devices,
            "approvals": [a.summary() for a in sorted(self._approvals.values(), key=lambda x: x.created_at)],
            "pairings": [
                {"code": p.code, "label": p.label, "ephemeral": p.ephemeral,
                 "createdAt": p.created_at, "expiresAt": p.expires_at}
                for p in self._pairings.values()
            ],
        }


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
    if not args or args[0] not in ("connect", "pair", "devices"):
        cmd = [str(exe), "connect"]
        if not any(a == "--server" or a.startswith("--server=") for a in args):
            cmd += ["--server", server]
        cmd += args
    else:
        cmd = [str(exe)] + args
    raise SystemExit(subprocess.call(cmd))

_main()
'''
