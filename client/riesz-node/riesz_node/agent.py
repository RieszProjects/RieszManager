"""Ephemeral, session-scoped local computer agent for `riesz-node`.

Security model:
- Every run generates a fresh in-memory Ed25519 keypair (NEVER written to disk).
- Each process binds strictly to ONE Riesz Manager session via a single-use pairing code.
- Closing the process permanently invalidates that key and removes all server state.
- Local `prefs.json` stores ONLY non-sensitive convenience memory per session ID:
  last granted directories and optional custom display name (never keys or tokens).
"""

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
from dataclasses import dataclass
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


def prefs_file() -> Path:
    return config_dir() / "prefs.json"


def _load_prefs() -> dict[str, Any]:
    path = prefs_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"sessions": {}}


def _save_prefs(data: dict[str, Any]) -> None:
    path = prefs_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def load_session_pref(session_id: str) -> dict[str, Any]:
    """Read local convenience memory (previous dirs / custom name) for `session_id`."""
    if not session_id:
        return {}
    entry = _load_prefs().get("sessions", {}).get(session_id)
    return dict(entry) if isinstance(entry, dict) else {}


def save_session_pref(
    session_id: str,
    *,
    dirs: list[Path] | None = None,
    custom_name: str | None = None,
    title: str | None = None,
) -> None:
    """Persist non-sensitive convenience preferences (dirs / customName) for `session_id`."""
    if not session_id:
        return
    data = _load_prefs()
    sessions = data.setdefault("sessions", {})
    rec = dict(sessions.get(session_id) or {})
    if dirs is not None:
        rec["dirs"] = [str(p) for p in dirs]
    if custom_name is not None:
        cleaned = " ".join(custom_name.strip().split())[:48]
        if cleaned:
            rec["customName"] = cleaned
    if title:
        rec["lastTitle"] = title[:80]
    rec["updatedAt"] = time.time()
    sessions[session_id] = rec
    # Keep prefs tidy (most recent 200 sessions)
    if len(sessions) > 200:
        ordered = sorted(sessions.items(), key=lambda kv: float((kv[1] or {}).get("updatedAt") or 0), reverse=True)
        data["sessions"] = dict(ordered[:200])
    _save_prefs(data)


def restore_valid_dirs(raw_dirs: list[str] | Any) -> list[Path]:
    """Filter previously saved directories to those that still exist and pass local policy."""
    if not isinstance(raw_dirs, list):
        return []
    out: list[Path] = []
    for raw in raw_dirs:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            resolved = normalize_roots([raw], allow_empty=False)[0]
        except PolicyError:
            continue
        if resolved not in out:
            out.append(resolved)
    return out


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


def pair_session(
    server: str,
    code: str,
    *,
    name: str = "",
    cli_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Exchange a one-time session pairing code for an ephemeral in-memory node identity.

    - Never writes private keys to disk.
    - Restores previous authorized directories & custom display name from `prefs.json`
      when re-binding a session this machine has connected to before.
    """
    base = normalize_server_url(server)
    seed, pub = ed.generate_keypair()
    host = socket.gethostname()
    cli_name = name.strip()
    payload = json.dumps({
        "code": code.strip(),
        "publicKey": ed.b64u_encode(pub),
        "name": (cli_name or host).strip(),
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

    session_id = str(data["sessionId"])
    session_title = str(data.get("sessionTitle") or session_id[:8])
    pref = load_session_pref(session_id)

    # Restore previous custom name if user renamed this session before (server resolves conflicts)
    desired_name = cli_name or str(pref.get("customName") or "").strip() or str(data.get("name") or host)

    # Restore previous authorized directories unless explicit --dir was given on CLI
    auto_restored = False
    if cli_dirs:
        initial_dirs = list(cli_dirs)
    else:
        restored = restore_valid_dirs(pref.get("dirs"))
        initial_dirs = restored
        auto_restored = bool(restored)

    if cli_name:
        save_session_pref(session_id, custom_name=cli_name, title=session_title)
    if cli_dirs:
        save_session_pref(session_id, dirs=initial_dirs, title=session_title)

    return {
        "nodeId": data["nodeId"],
        "name": desired_name,
        "server": base,
        "seed": ed.b64u_encode(seed),
        "publicKey": ed.b64u_encode(pub),
        "sessionId": session_id,
        "sessionTitle": session_title,
        "harness": str(data.get("harness") or ""),
        "project": str(data.get("project") or ""),
        "mode": str(data.get("mode") or "auto"),
        "dirs": initial_dirs,
        "autoRestoredDirs": auto_restored,
    }


@dataclass
class PendingApproval:
    approval_id: str
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
    command: str
    cwd: str
    reason: str
    created_at: float
    future: asyncio.Future[bool]


@dataclass
class CommandEntry:
    cmd_id: str
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
    """Async state machine maintaining a single ephemeral, session-scoped reverse WSS tunnel."""

    def __init__(
        self,
        *,
        session: dict[str, Any],
        dirs: list[Path] | None = None,
        max_mode: str = "auto",
        note: str = "",
        interactive: bool = True,
        on_update: Callable[[], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.node_id = str(session["nodeId"])
        self.name = str(session["name"])
        self.server = normalize_server_url(str(session["server"]))
        self.seed = ed.b64u_decode(str(session["seed"]))
        self.session_id = str(session["sessionId"])
        self.session_title = str(session.get("sessionTitle") or self.session_id[:8])
        self.harness = str(session.get("harness") or "")
        self.project = str(session.get("project") or "")
        self.max_mode = max_mode if max_mode in MODE_RANK else "auto"
        req_mode = str(session.get("mode") or "auto")
        self.mode = req_mode if req_mode in MODE_RANK and MODE_RANK[req_mode] <= MODE_RANK[self.max_mode] else self.max_mode
        self.dirs: list[Path] = list(dirs if dirs is not None else (session.get("dirs") or []))
        self.auto_restored_dirs = bool(session.get("autoRestoredDirs"))
        self.note = note
        self.interactive = interactive
        self.on_update = on_update
        self.on_log = on_log

        self.status = "disconnected"     # "connecting" | "online" | "disconnected" | "kicked"
        self.status_detail = "正在初始化…"
        self.connected_at: float | None = None
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
                    "dirs": [str(p) for p in self.dirs],
                    "maxMode": self.max_mode,
                    "note": self.note,
                    "version": "2",
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

                self.name = str(welcome.get("name") or self.name)
                self.session_title = str(welcome.get("sessionTitle") or self.session_title)
                self.harness = str(welcome.get("harness") or self.harness)
                self.project = str(welcome.get("project") or self.project)
                self.mode = str(welcome.get("mode") or self.mode)
                self.status = "online"
                self.connected_at = time.time()
                if self.auto_restored_dirs and self.dirs:
                    self.status_detail = f"已连接并自动恢复上次授权目录（{len(self.dirs)} 个）"
                elif self.dirs:
                    self.status_detail = f"已连接（已授权 {len(self.dirs)} 个目录）"
                else:
                    self.status_detail = "已连接，请按 e 选择允许本会话访问的目录"
                self._emit()
                self._log(f"[在线] 节点「{self.name}」已绑定会话「{self.session_title}」({self.session_id[:8]})")

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
            pass
        except ConnectionClosed as exc:
            if self.status != "kicked":
                self.status = "disconnected"
                self.status_detail = f"连接已关闭（code={exc.code}）。本次临时连接已结束，按 Ctrl+Q 退出。"
                self._log(f"[断开] {self.status_detail}")
        except Exception as exc:  # noqa: BLE001
            if self.status != "kicked":
                self.status = "disconnected"
                self.status_detail = f"连接断开：{exc}"
                self._log(f"[断开] {self.status_detail}")
        finally:
            self._ws = None
            await self._kill_all_running()
            self.pending_approvals.clear()
            for lc in list(self.pending_local_confirms):
                if not lc.future.done():
                    lc.future.set_result(False)
            self.pending_local_confirms.clear()
            self._emit()

    # ---- incoming protocol --------------------------------------------------

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "kicked":
            self.status = "kicked"
            self.status_detail = str(msg.get("reason") or "已断开连接") + "（按 Ctrl+Q 退出）"
            self._log(f"[断开] {self.status_detail}")
            self._emit()
            if self._ws is not None:
                await self._ws.close()
        elif mtype == "renamed":
            new_name = str(msg.get("name") or "").strip()
            if new_name:
                self.name = new_name
                save_session_pref(self.session_id, custom_name=new_name, title=self.session_title)
                self._log(f"[改名] 本机名称已更新为：{new_name}")
                self._emit()
        elif mtype == "mode_updated":
            new_mode = str(msg.get("mode") or "")
            if new_mode in MODE_RANK and MODE_RANK[new_mode] <= MODE_RANK[self.max_mode]:
                self.mode = new_mode
                self._log(f"[权限] 权限模式已调整为：{new_mode}")
                self._emit()
        elif mtype == "approval_request":
            ap = PendingApproval(
                approval_id=str(msg.get("id") or ""),
                action=str(msg.get("action") or "exec"),
                command=str(msg.get("command") or ""),
                cwd=str(msg.get("cwd") or ""),
                risk=str(msg.get("risk") or "medium"),
                reason=str(msg.get("reason") or ""),
                created_at=time.time(),
            )
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

    # ---- local user actions -------------------------------------------------

    async def update_dirs(self, dirs: list[str]) -> list[Path]:
        resolved = normalize_roots(dirs, allow_empty=False)
        self.dirs = resolved
        save_session_pref(self.session_id, dirs=resolved, title=self.session_title)
        await self._send({"type": "dirs_update", "dirs": [str(p) for p in resolved]})
        self.status_detail = f"已授权 {len(resolved)} 个目录"
        self._log(f"[更新目录] 允许目录已更新为：{', '.join(str(p) for p in resolved)}")
        self._emit()
        return resolved

    async def rename(self, new_name: str) -> str:
        cleaned = " ".join(new_name.strip().split())[:48]
        if not cleaned:
            raise PolicyError("名称不能为空")
        self.name = cleaned
        save_session_pref(self.session_id, custom_name=cleaned, title=self.session_title)
        await self._send({"type": "rename", "name": cleaned})
        self._log(f"[改名] 已请求改名为：{cleaned}")
        self._emit()
        return cleaned

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

    def _record_cmd(self, entry: CommandEntry) -> None:
        self.commands.insert(0, entry)
        self._cmd_by_id[entry.cmd_id] = entry
        if len(self.commands) > 100:
            old = self.commands.pop()
            self._cmd_by_id.pop(old.cmd_id, None)

    async def _request_local_confirm(self, cmd_id: str, command: str, cwd: str, reason: str) -> bool:
        if not self.interactive:
            return False
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        lc = LocalConfirm(
            confirm_id=f"lc-{cmd_id}", command=command, cwd=cwd,
            reason=reason, created_at=time.time(), future=fut,
        )
        self.pending_local_confirms.append(lc)
        self._log(f"[本机拦截确认] {reason} -> {command}")
        self._emit()
        try:
            return await asyncio.wait_for(fut, timeout=LOCAL_CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            self.pending_local_confirms = [x for x in self.pending_local_confirms if x.confirm_id != lc.confirm_id]
            self._emit()
            return False

    # ---- execution & transfer ----------------------------------------------

    async def _run_exec(self, msg: dict[str, Any]) -> None:
        cid = str(msg.get("cmdId") or "")
        command = str(msg.get("command") or "")
        raw_cwd = str(msg.get("cwd") or "")
        timeout = max(5, min(int(msg.get("timeout") or 120), 600))
        risk = str(msg.get("risk") or "low")
        risk_reason = str(msg.get("riskReason") or "")
        approval_id = str(msg.get("approvalId") or "")

        if not self.dirs:
            await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": "本地拒绝：尚未为本会话授权任何允许目录"})
            return

        check = evaluate_local_command(
            command,
            raw_cwd or str(self.dirs[0]),
            self.dirs,
            self.mode,
            server_approved_id=approval_id or None,
            seen_approvals=self._seen_approvals,
        )
        if check.verdict == "deny" or check.cwd is None:
            entry = CommandEntry(
                cmd_id=cid, action="exec", command=command, cwd=str(check.cwd or raw_cwd or self.dirs[0]),
                risk="high", risk_reason=check.reason, status="denied",
                started_at=time.time(), ended_at=time.time(), exit_code=-1, output=check.reason,
            )
            self._record_cmd(entry)
            self._log(f"[本地硬拦截] {check.reason} -> {command}")
            self._emit()
            await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": check.reason})
            return

        safe_cwd = check.cwd
        needs_local_confirm = check.verdict == "local_confirm"
        local_reason = check.reason

        entry = CommandEntry(
            cmd_id=cid, action="exec", command=command, cwd=str(safe_cwd),
            risk="high" if needs_local_confirm else risk,
            risk_reason=local_reason or risk_reason,
            status="confirming" if needs_local_confirm else "running",
            started_at=time.time(),
        )
        self._record_cmd(entry)
        self._emit()

        if needs_local_confirm:
            allowed = await self._request_local_confirm(cid, command, str(safe_cwd), local_reason)
            if not allowed:
                entry.status = "denied"
                entry.ended_at = time.time()
                entry.exit_code = -1
                entry.output = f"本地电脑端拒绝了该高危命令：{local_reason}"
                self._emit()
                await self._send({"type": "exec_done", "cmdId": cid, "exitCode": -1, "error": entry.output})
                return
            entry.status = "running"
            self._emit()

        self._log(f"[执行] ({safe_cwd}) $ {command}")

        sent_bytes = 0
        truncated_notice = False
        chunk_buf: list[str] = []
        last_flush = time.monotonic()

        async def flush_chunks(force: bool = False) -> None:
            nonlocal last_flush
            if not chunk_buf:
                return
            now = time.monotonic()
            if not force and (now - last_flush) < 0.15 and sum(len(x) for x in chunk_buf) < 4096:
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
        dest_raw = str(msg.get("destPath") or "")
        url_path = str(msg.get("url") or "")
        expected_sha = str(msg.get("sha256") or "") or None
        if not self.dirs:
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：尚未为本会话授权允许目录"})
            return
        if self.mode == "readonly":
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：只读模式禁止写入文件"})
            return

        entry = CommandEntry(
            cmd_id=tid, action="push", command=f"接收文件 -> {dest_raw}", cwd=str(self.dirs[0]),
            risk="low", risk_reason="文件推送", status="running", started_at=time.time(),
        )
        self._record_cmd(entry)
        self._emit()
        try:
            full_url = safe_transfer_url(self.server, url_path)
            dest = resolve_allowed_path(dest_raw, self.dirs, must_exist=False, strict_below_root=True)
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
        src_raw = str(msg.get("srcPath") or "")
        url_path = str(msg.get("url") or "")
        if not self.dirs:
            await self._send({"type": "transfer_done", "transferId": tid, "ok": False, "error": "本地拒绝：尚未为本会话授权允许目录"})
            return

        entry = CommandEntry(
            cmd_id=tid, action="pull", command=f"上传文件 <- {src_raw}", cwd=str(self.dirs[0]),
            risk="low", risk_reason="文件拉取", status="running", started_at=time.time(),
        )
        self._record_cmd(entry)
        self._emit()
        try:
            full_url = safe_transfer_url(self.server, url_path)
            src = resolve_allowed_path(src_raw, self.dirs, must_exist=True, strict_below_root=True)
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
