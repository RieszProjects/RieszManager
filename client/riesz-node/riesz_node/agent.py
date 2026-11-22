"""Device credential store and hardened reverse WebSocket agent for `riesz-node`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform as plat
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ed25519_pure as ed
from .executor import RunningProcess, download_file_sync, run_command, safe_transfer_url, upload_file_sync
from .policy import PolicyError, evaluate_local_command, normalize_roots, resolve_allowed_path

MODES = ("readonly", "ask", "auto", "full")
MODE_RANK = {m: i for i, m in enumerate(MODES)}
MAX_CONCURRENT_CMDS = 4
MAX_WS_OUTPUT_BYTES = 256 * 1024
LOCAL_CONFIRM_TIMEOUT = 180.0


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "riesz-node"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "riesz-node"
    return Path.home() / ".config" / "riesz-node"


def devices_file() -> Path:
    return config_dir() / "devices.json"


def load_devices() -> list[dict[str, Any]]:
    path = devices_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("devices") or [] if isinstance(data, dict) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def save_device(entry: dict[str, Any]) -> None:
    path = devices_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
    devices = [d for d in load_devices() if d.get("nodeId") != entry.get("nodeId")]
    devices.insert(0, entry)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"devices": devices}, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def normalize_server_url(server: str) -> str:
    url = server.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def ws_url_for(server: str) -> str:
    base = normalize_server_url(server)
    if base.startswith("https://"):
        return "wss://" + base[8:] + "/ws/node"
    return "ws://" + base[7:] + "/ws/node"


def os_description() -> str:
    sys_name = plat.system()
    rel = plat.release()
    mach = plat.machine()
    return f"{sys_name} {rel} ({mach})".strip()


def pair_device(server: str, code: str, *, name: str = "", ephemeral: bool = False) -> dict[str, Any]:
    """Generate a local Ed25519 keypair and pair with Riesz Manager using a one-time pairing code."""
    base = normalize_server_url(server)
    seed, pub = ed.generate_keypair()
    host = socket.gethostname()
    payload = json.dumps({
        "code": code.strip(),
        "publicKey": ed.b64u_encode(pub),
        "name": (name or host).strip(),
        "os": os_description(),
        "platform": sys.platform,
        "hostname": host,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/nodes/register",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "riesz-node/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body).get("error") or body
        except ValueError:
            msg = body
        raise RuntimeError(f"配对失败（HTTP {exc.code}）：{msg}") from None
    entry = {
        "nodeId": data["nodeId"],
        "name": data.get("name") or name or host,
        "server": base,
        "seed": ed.b64u_encode(seed),
        "publicKey": ed.b64u_encode(pub),
        "ephemeral": bool(data.get("ephemeral") or ephemeral),
        "pairedAt": time.time(),
    }
    if not entry["ephemeral"]:
        save_device(entry)
    return entry


@dataclass
class BoundSession:
    session_id: str
    title: str
    harness: str
    project: str
    mode: str
    bound_at: float
    dirs: list[Path] = field(default_factory=list)


@dataclass
class PendingBind:
    bind_id: str
    session_id: str
    session_title: str
    harness: str
    project: str
    mode: str
    created_at: float
    is_update: bool = False


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    session_title: str
    action: str
    command: str
    cwd: str
    risk: str
    reason: str
    created_at: float


@dataclass
class LocalConfirm:
    """High-risk or unverified command intercepted locally; requires human confirmation AT THIS COMPUTER."""
    confirm_id: str
    session_id: str
    session_title: str
    command: str
    cwd: str
    reason: str
    created_at: float
    future: asyncio.Future[bool]


@dataclass
class CommandEntry:
    cmd_id: str
    session_id: str
    session_title: str
    action: str          # "exec" | "push" | "pull"
    command: str
    cwd: str
    risk: str
    risk_reason: str
    status: str          # "running" | "done" | "error" | "killed" | "denied" | "confirming"
    started_at: float
    ended_at: float | None = None
    exit_code: int | None = None
    output: str = ""
    proc: RunningProcess | None = None


class NodeAgent:
    """Async state machine maintaining the outbound WSS connection and local execution."""

    def __init__(
        self,
        *,
        device: dict[str, Any],
        roots: list[Path],
        max_mode: str = "auto",
        note: str = "",
        auto_accept_bind: bool = False,
        interactive: bool = True,
        on_update: Callable[[], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.device = device
        self.node_id = str(device["nodeId"])
        self.name = str(device["name"])
        self.server = normalize_server_url(str(device["server"]))
        self.seed = ed.b64u_decode(str(device["seed"]))
        self.roots = roots
        self.max_mode = max_mode if max_mode in MODE_RANK else "auto"
        self.note = note
        self.auto_accept_bind = auto_accept_bind
        self.interactive = interactive
        self.on_update = on_update
        self.on_log = on_log

        self.status = "disconnected"     # "connecting" | "online" | "disconnected" | "kicked"
        self.status_detail = "正在初始化…"
        self.connected_at: float | None = None
        self.bindings: dict[str, BoundSession] = {}
        self.pending_binds: list[PendingBind] = []
        self.pending_approvals: list[PendingApproval] = []
        self.pending_local_confirms: list[LocalConfirm] = []
        self.commands: list[CommandEntry] = []
        self._cmd_by_id: dict[str, CommandEntry] = {}
        self._seen_approvals: set[str] = set()
        self._ws: Any = None
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_CMDS)

    def _emit(self) -> None:
        if self.on_update:
            self.on_update()

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def stop(self) -> None:
        self._stop.set()
        for lc in list(self.pending_local_confirms):
            if not lc.future.done():
                lc.future.set_result(False)
        self.pending_local_confirms.clear()
        await self._kill_all_running()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        for t in list(self._tasks):
            t.cancel()

    async def _kill_all_running(self) -> None:
        for entry in list(self.commands):
            if entry.proc is not None and entry.status == "running":
                with contextlib.suppress(Exception):
                    await entry.proc.kill()

    async def run(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        url = ws_url_for(self.server)
        backoff = 1.0
        while not self._stop.is_set():
            self.status = "connecting"
            self.status_detail = f"正在连接 {url}"
            self._emit()
            self._log(f"[连接] {self.status_detail}")
            try:
                async with websockets.connect(
                    url, ping_interval=25, ping_timeout=20, max_size=16 * 1024 * 1024,
                    user_agent_header="riesz-node/0.1",
                ) as ws:
                    self._ws = ws
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    chal = json.loads(raw)
                    if chal.get("type") != "challenge":
                        raise RuntimeError(f"意外握手消息：{chal}")
                    payload = f"riesz-node-auth:{self.node_id}:{chal['nonce']}:{chal['ts']}".encode("utf-8")
                    sig = ed.b64u_encode(ed.sign(self.seed, payload))
                    await self._send({
                        "type": "hello",
                        "nodeId": self.node_id,
                        "signature": sig,
                        "name": self.name,
                        "os": os_description(),
                        "platform": sys.platform,
                        "hostname": socket.gethostname(),
                        "dirs": [str(r) for r in self.roots],
                        "maxMode": self.max_mode,
                        "note": self.note,
                        "version": "1",
                    })
                    welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if welcome.get("type") == "auth_error":
                        self.status = "kicked"
                        self.status_detail = f"认证被拒：{welcome.get('error')}"
                        self._emit()
                        self._log(f"[错误] {self.status_detail}")
                        return
                    if welcome.get("type") != "welcome":
                        raise RuntimeError(f"握手失败：{welcome}")

                    backoff = 1.0
                    self.status = "online"
                    self.connected_at = time.time()
                    self.status_detail = f"已连接（节点 {self.name}，等待网页端绑定会话）"
                    self._emit()
                    self._log(f"[在线] {self.status_detail}")

                    async for raw_msg in ws:
                        if not isinstance(raw_msg, str):
                            continue
                        try:
                            msg = json.loads(raw_msg)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            await self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code in (4000, 4401):
                    self.status = "kicked"
                    self.status_detail = f"连接已被服务端终止（code={code}）"
                    self._log(f"[终止] {self.status_detail}")
                else:
                    self._log(f"[断线] {exc}")
                    self.status_detail = f"连接断开：{exc}"
            except Exception as exc:  # noqa: BLE001
                self._log(f"[断线] {exc}")
                self.status_detail = f"连接断开：{exc}"
            finally:
                self._ws = None
                self.bindings.clear()
                self.pending_binds.clear()
                self.pending_approvals.clear()
                for lc in list(self.pending_local_confirms):
                    if not lc.future.done():
                        lc.future.set_result(False)
                self.pending_local_confirms.clear()
                await self._kill_all_running()
                if self.status != "kicked":
                    self.status = "disconnected"
                self._emit()

            if self._stop.is_set() or self.status == "kicked" or self.device.get("ephemeral"):
                break
            self._log(f"[重连] {backoff:.0f} 秒后重试…")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 30.0)

    # ---- incoming protocol --------------------------------------------------

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "kicked":
            self.status = "kicked"
            self.status_detail = str(msg.get("reason") or "服务端主动断开了连接")
            self._log(f"[断开] {self.status_detail}")
            self._emit()
            if self._ws is not None:
                await self._ws.close()
        elif mtype == "bind_request":
            sid = str(msg.get("sessionId") or "")
            req = PendingBind(
                bind_id=str(msg.get("bindId") or ""),
                session_id=sid,
                session_title=str(msg.get("sessionTitle") or "未命名会话"),
                harness=str(msg.get("harness") or ""),
                project=str(msg.get("project") or ""),
                mode=str(msg.get("mode") or "auto"),
                created_at=time.time(),
                is_update=sid in self.bindings,
            )
            if self.auto_accept_bind and self.roots:
                await self.respond_bind(req, allow=True, dirs=[str(r) for r in self.roots])
            elif not self.interactive:
                await self.respond_bind(req, allow=False)
            else:
                self.pending_binds.append(req)
                self._log(f"[绑定请求] 会话「{req.session_title}」申请绑定（权限={req.mode}），等待选择目录并确认")
                self._emit()
        elif mtype == "unbind":
            sid = str(msg.get("sessionId") or "")
            removed = self.bindings.pop(sid, None)
            self.pending_approvals = [a for a in self.pending_approvals if a.session_id != sid]
            for lc in list(self.pending_local_confirms):
                if lc.session_id == sid:
                    self.pending_local_confirms.remove(lc)
                    if not lc.future.done():
                        lc.future.set_result(False)
            if removed:
                self._log(f"[解绑] 会话「{removed.title}」已解除绑定")
            self._emit()
        elif mtype == "approval_request":
            ap = PendingApproval(
                approval_id=str(msg.get("id") or ""),
                session_id=str(msg.get("sessionId") or ""),
                session_title=str(msg.get("sessionTitle") or ""),
                action=str(msg.get("action") or "exec"),
                command=str(msg.get("command") or ""),
                cwd=str(msg.get("cwd") or ""),
                risk=str(msg.get("risk") or "medium"),
                reason=str(msg.get("reason") or ""),
                created_at=time.time(),
            )
            if ap.session_id not in self.bindings:
                await self._send({"type": "approval_response", "approvalId": ap.approval_id, "allow": False})
                return
            self.pending_approvals.append(ap)
            self._log(f"[待审批] [{ap.risk}] {ap.command}（{ap.reason}）")
            self._emit()
        elif mtype == "approval_resolved":
            aid = str(msg.get("approvalId") or "")
            if msg.get("allow") and aid:
                self._seen_approvals.add(aid)
                if len(self._seen_approvals) > 200:
                    self._seen_approvals = set(list(self._seen_approvals)[-100:])
            self.pending_approvals = [a for a in self.pending_approvals if a.approval_id != aid]
            self._emit()
        elif mtype == "exec":
            t = asyncio.create_task(self._run_exec(msg))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        elif mtype == "exec_kill":
            await self.kill_command(str(msg.get("cmdId") or ""))
        elif mtype == "file_pull_from_server":
            t = asyncio.create_task(self._run_pull_from_server(msg))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        elif mtype == "file_push_to_server":
            t = asyncio.create_task(self._run_push_to_server(msg))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    # ---- user actions -------------------------------------------------------

    async def respond_bind(self, req: PendingBind, *, allow: bool, dirs: list[str] | None = None) -> None:
        eff = req.mode if MODE_RANK.get(req.mode, 9) <= MODE_RANK[self.max_mode] else self.max_mode
        resolved_dirs: list[Path] = []
        if allow:
            raw_dirs = dirs if dirs is not None else [str(r) for r in self.roots]
            resolved_dirs = normalize_roots(raw_dirs, allow_empty=False)
        self.pending_binds = [b for b in self.pending_binds if b.bind_id != req.bind_id]
        if allow:
            self.bindings[req.session_id] = BoundSession(
                session_id=req.session_id, title=req.session_title, harness=req.harness,
                project=req.project, mode=eff, bound_at=time.time(), dirs=resolved_dirs,
            )
            self._log(f"[已绑定] 会话「{req.session_title}」（权限={eff}，目录={', '.join(str(p) for p in resolved_dirs)}）")
        else:
            self._log(f"[已拒绝绑定] 会话「{req.session_title}」")
        await self._send({
            "type": "bind_response", "bindId": req.bind_id, "allow": allow,
            "mode": eff,
            **({"dirs": [str(p) for p in resolved_dirs]} if allow else {"reason": "用户在本地电脑端拒绝了绑定请求"}),
        })
        self._emit()

    async def update_binding_dirs(self, session_id: str, dirs: list[str]) -> list[Path]:
        binding = self.bindings.get(session_id)
        if binding is None:
            raise PolicyError("该会话当前未绑定")
        resolved = normalize_roots(dirs, allow_empty=False)
        binding.dirs = resolved
        await self._send({"type": "bind_update", "sessionId": session_id, "dirs": [str(p) for p in resolved]})
        self._log(f"[更新目录] 会话「{binding.title}」允许目录已更新为：{', '.join(str(p) for p in resolved)}")
        self._emit()
        return resolved

    async def unbind_session(self, session_id: str) -> None:
        removed = self.bindings.pop(session_id, None)
        if removed is None:
            return
        self.pending_approvals = [a for a in self.pending_approvals if a.session_id != session_id]
        for lc in list(self.pending_local_confirms):
            if lc.session_id == session_id:
                self.pending_local_confirms.remove(lc)
                if not lc.future.done():
                    lc.future.set_result(False)
        for entry in list(self.commands):
            if entry.session_id == session_id and entry.status == "running" and entry.proc:
                await entry.proc.kill()
        await self._send({"type": "unbind", "sessionId": session_id})
        self._log(f"[解绑] 已主动解除会话「{removed.title}」的绑定")
        self._emit()

    async def respond_approval(self, ap: PendingApproval, *, allow: bool) -> None:
        self.pending_approvals = [a for a in self.pending_approvals if a.approval_id != ap.approval_id]
        if allow:
            self._seen_approvals.add(ap.approval_id)
        await self._send({"type": "approval_response", "approvalId": ap.approval_id, "allow": allow})
        self._log(f"[审批] {'已允许' if allow else '已拒绝'}：{ap.command}")
        self._emit()

    async def respond_local_confirm(self, lc: LocalConfirm, *, allow: bool) -> None:
        self.pending_local_confirms = [x for x in self.pending_local_confirms if x.confirm_id != lc.confirm_id]
        if not lc.future.done():
            lc.future.set_result(bool(allow))
        self._log(f"[本机安全复核] {'已放行' if allow else '已拒绝'}：{lc.command}")
        self._emit()

    async def kill_command(self, cmd_id: str) -> bool:
        entry = self._cmd_by_id.get(cmd_id)
        if entry is None or entry.proc is None or entry.status != "running":
            return False
        await entry.proc.kill()
        self._log(f"[终止] 已终止命令：{entry.command}")
        self._emit()
        return True

    # ---- execution & file transfer -----------------------------------------

    def _record_cmd(self, entry: CommandEntry) -> None:
        self.commands.insert(0, entry)
        self._cmd_by_id[entry.cmd_id] = entry
        if len(self.commands) > 120:
            old = self.commands.pop()
            self._cmd_by_id.pop(old.cmd_id, None)

    async def _run_exec(self, msg: dict[str, Any]) -> None:
        cid = str(msg.get("cmdId") or "")
        sid = str(msg.get("sessionId") or "")
        command = str(msg.get("command") or "")
        cwd_raw = str(msg.get("cwd") or "")
        timeout = max(5, min(int(msg.get("timeout") or 120), 600))
        approval_id = str(msg.get("approvalId") or "") or None
        binding = self.bindings.get(sid)
        if binding is None:
            await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": "本地拒绝：该会话未绑定此电脑"})
            return
        if not binding.dirs:
            await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": "本地拒绝：该会话尚未授权任何本地目录"})
            return

        entry = CommandEntry(
            cmd_id=cid, session_id=sid, session_title=binding.title, action="exec",
            command=command, cwd=cwd_raw or str(binding.dirs[0]),
            risk=str(msg.get("risk") or "low"), risk_reason=str(msg.get("riskReason") or ""),
            status="running", started_at=time.time(),
        )
        self._record_cmd(entry)
        self._emit()

        check = evaluate_local_command(
            command, cwd_raw, binding.dirs, binding.mode,
            server_approved_id=approval_id, seen_approvals=self._seen_approvals,
        )
        if check.verdict == "deny" or check.cwd is None:
            entry.status = "denied"
            entry.ended_at = time.time()
            entry.output = check.reason
            self._log(f"[硬拦截] {check.reason} | {command}")
            self._emit()
            await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": check.reason})
            return

        safe_cwd = check.cwd
        entry.cwd = str(safe_cwd)

        if check.verdict == "local_confirm":
            if not self.interactive:
                msg_err = f"{check.reason}（非交互 --plain 模式默认拒绝高危/未审批命令）"
                entry.status = "denied"
                entry.ended_at = time.time()
                entry.output = msg_err
                self._log(f"[拦截] {msg_err}")
                self._emit()
                await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": msg_err})
                return
            entry.status = "confirming"
            entry.risk = "high"
            entry.risk_reason = check.reason
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            lc = LocalConfirm(
                confirm_id=cid, session_id=sid, session_title=binding.title,
                command=command, cwd=str(safe_cwd), reason=check.reason,
                created_at=time.time(), future=fut,
            )
            self.pending_local_confirms.append(lc)
            self._log(f"[本机复核] {check.reason} | $ {command}")
            self._emit()
            try:
                allowed = await asyncio.wait_for(fut, timeout=LOCAL_CONFIRM_TIMEOUT)
            except asyncio.TimeoutError:
                self.pending_local_confirms = [x for x in self.pending_local_confirms if x.confirm_id != cid]
                allowed = False
            if not allowed:
                entry.status = "denied"
                entry.ended_at = time.time()
                entry.output = f"本地电脑端拒绝了该高危命令（{check.reason}）"
                self._emit()
                await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": entry.output})
                return
            entry.status = "running"
            self._emit()

        if approval_id:
            self._seen_approvals.discard(approval_id)

        self._log(f"[执行] ({binding.title}) $ {command}")
        sent_bytes = 0
        truncated_notice = False
        chunk_buf: list[str] = []
        last_flush = time.monotonic()

        async def flush_chunks(force: bool = False) -> None:
            nonlocal last_flush
            if not chunk_buf:
                return
            now = time.monotonic()
            if not force and now - last_flush < 0.12:
                return
            joined = "".join(chunk_buf)
            chunk_buf.clear()
            last_flush = now
            with contextlib.suppress(Exception):
                await self._send({"type": "exec_chunk", "cmdId": cid, "data": joined})

        async def on_chunk(text: str) -> None:
            nonlocal sent_bytes, truncated_notice
            entry.output = (entry.output + text)[-128 * 1024:]
            self._emit()
            if sent_bytes < MAX_WS_OUTPUT_BYTES:
                sent_bytes += len(text.encode("utf-8", errors="ignore"))
                chunk_buf.append(text)
                await flush_chunks(force=False)
            elif not truncated_notice:
                truncated_notice = True
                chunk_buf.append("\n[输出超过 256KB 上限，后续输出仅保留在本地最新尾部]\n")
                await flush_chunks(force=True)

        def on_started(proc: RunningProcess) -> None:
            entry.proc = proc

        async with self._sem:
            try:
                code, killed, err = await run_command(command, safe_cwd, timeout=timeout, on_chunk=on_chunk, on_started=on_started)
                await flush_chunks(force=True)
                entry.exit_code = code
                entry.ended_at = time.time()
                if killed:
                    entry.status = "killed"
                elif err or (code is not None and code != 0):
                    entry.status = "error"
                else:
                    entry.status = "done"
                if err:
                    entry.output = (entry.output + ("\n" if entry.output else "") + err)[-128 * 1024:]
                self._log(f"[完成] exit={code} ({entry.status}) $ {command}")
                self._emit()
                await self._send({"type": "exec_done", "cmdId": cid, "exitCode": code, "killed": killed, "error": err})
            except Exception as exc:  # noqa: BLE001
                entry.status = "error"
                entry.ended_at = time.time()
                entry.output += f"\n{exc}"
                self._emit()
                with contextlib.suppress(Exception):
                    await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": str(exc)})

    async def _run_pull_from_server(self, msg: dict[str, Any]) -> None:
        tid = str(msg.get("transferId") or "")
        sid = str(msg.get("sessionId") or "")
        dest_raw = str(msg.get("destPath") or "")
        url_path = str(msg.get("url") or "")
        expected_sha = str(msg.get("sha256") or "") or None
        binding = self.bindings.get(sid)
        if binding is None:
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：会话未绑定"})
            return
        if not binding.dirs:
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：该会话未授权允许目录"})
            return
        if binding.mode == "readonly":
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：只读模式禁止写入文件"})
            return

        entry = CommandEntry(
            cmd_id=tid, session_id=sid, session_title=binding.title, action="push",
            command=f"接收文件 -> {dest_raw}", cwd=str(binding.dirs[0]),
            risk="low", risk_reason="文件推送", status="running", started_at=time.time(),
        )
        self._record_cmd(entry)
        self._emit()
        try:
            full_url = safe_transfer_url(self.server, url_path)
            dest = resolve_allowed_path(dest_raw, binding.dirs, must_exist=False, strict_below_root=True)
            size, digest = await asyncio.to_thread(download_file_sync, full_url, dest, expected_sha)
            entry.status = "done"
            entry.exit_code = 0
            entry.ended_at = time.time()
            entry.output = f"已写入 {dest}（{size} 字节，sha256={digest[:12]}）"
            self._log(f"[接收文件] {entry.output}")
            self._emit()
            await self._send({"type": "transfer_done", "transferId": tid, "ok": True, "path": str(dest), "size": size, "sha256": digest})
        except Exception as exc:  # noqa: BLE001
            entry.status = "error"
            entry.ended_at = time.time()
            entry.output = str(exc)
            self._log(f"[接收失败] {exc}")
            self._emit()
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": str(exc)})

    async def _run_push_to_server(self, msg: dict[str, Any]) -> None:
        tid = str(msg.get("transferId") or "")
        sid = str(msg.get("sessionId") or "")
        src_raw = str(msg.get("srcPath") or "")
        url_path = str(msg.get("url") or "")
        binding = self.bindings.get(sid)
        if binding is None:
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：会话未绑定"})
            return
        if not binding.dirs:
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：该会话未授权允许目录"})
            return

        entry = CommandEntry(
            cmd_id=tid, session_id=sid, session_title=binding.title, action="pull",
            command=f"上传文件 <- {src_raw}", cwd=str(binding.dirs[0]),
            risk="low", risk_reason="文件拉取", status="running", started_at=time.time(),
        )
        self._record_cmd(entry)
        self._emit()
        try:
            full_url = safe_transfer_url(self.server, url_path)
            src = resolve_allowed_path(src_raw, binding.dirs, must_exist=True, strict_below_root=True)
            size, digest = await asyncio.to_thread(upload_file_sync, full_url, src)
            entry.status = "done"
            entry.exit_code = 0
            entry.ended_at = time.time()
            entry.output = f"已上传 {src}（{size} 字节，sha256={digest[:12]}）"
            self._log(f"[上传文件] {entry.output}")
            self._emit()
            await self._send({"type": "transfer_done", "transferId": tid, "ok": True, "size": size, "sha256": digest})
        except Exception as exc:  # noqa: BLE001
            entry.status = "error"
            entry.ended_at = time.time()
            entry.output = str(exc)
            self._log(f"[上传失败] {exc}")
            self._emit()
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": str(exc)})
