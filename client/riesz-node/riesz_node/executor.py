"""Cross-platform async command execution and strict streaming file transfer."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

MAX_TRANSFER_BYTES = 200 * 1024 * 1024
TRANSFER_PATH_RE = re.compile(r"^/api/nodes/transfer/[A-Za-z0-9_-]{16,128}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow HTTP redirects on file transfers (prevents compromised server from redirecting uploads to third parties)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, f"重定向被本地安全策略禁止（-> {newurl}）", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect())


def safe_transfer_url(server_base: str, url_path: str) -> str:
    """Strictly validate that `url_path` is a safe same-origin transfer path (blocks SSRF / `@evil.com` tricks)."""
    if not TRANSFER_PATH_RE.fullmatch(url_path or ""):
        raise ValueError(f"非法传输路径：{url_path!r}")
    base = server_base.rstrip("/")
    full = base + url_path
    p_base = urllib.parse.urlsplit(base)
    p_full = urllib.parse.urlsplit(full)
    if p_full.scheme != p_base.scheme or p_full.netloc != p_base.netloc:
        raise ValueError("传输地址源站不匹配，已拒绝")
    return full


class RunningProcess:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.killed = False

    async def kill(self) -> None:
        self.killed = True
        if self.proc.returncode is not None:
            return
        if sys.platform == "win32":
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(self.proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
            except OSError:
                self.proc.kill()
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                self.proc.kill()


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh:
            # Force UTF-8 output encoding on Windows PowerShell so Chinese logs don't garble.
            wrapped = f"[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; {command}"
            return [pwsh, "-NoProfile", "-NonInteractive", "-Command", wrapped]
        return ["cmd.exe", "/c", command]
    bash = shutil.which("bash")
    if bash:
        return [bash, "-lc", command]
    return ["/bin/sh", "-c", command]


def _sanitized_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("RIESZ_")}
    env.setdefault("PYTHONUTF8", "1")
    return env


async def run_command(
    command: str,
    cwd: Path,
    *,
    timeout: int = 120,
    on_chunk: Callable[[str], Awaitable[None]],
    on_started: Callable[[RunningProcess], None] | None = None,
) -> tuple[int | None, bool, str | None]:
    """Run a shell command with real-time combined output streaming. Returns (exit_code, killed, error)."""
    argv = _shell_argv(command)
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": _sanitized_env(),
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    runner = RunningProcess(proc)
    if on_started:
        on_started(runner)

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            await on_chunk(chunk.decode("utf-8", errors="replace"))

    pump_task = asyncio.create_task(pump())
    err: str | None = None
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        err = f"命令执行超时（>{timeout}s）"
        await runner.kill()
    finally:
        try:
            await asyncio.wait_for(pump_task, timeout=3.0)
        except asyncio.TimeoutError:
            pump_task.cancel()
    return proc.returncode, runner.killed, err


def download_file_sync(url: str, dest: Path, expected_sha256: str | None = None, max_bytes: int = MAX_TRANSFER_BYTES) -> tuple[int, str]:
    """Stream-download a file from Riesz Manager into `dest`, enforcing max size, no redirects, and SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.rz.tmp")
    h = hashlib.sha256()
    total = 0
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "riesz-node/0.1"})
    try:
        with _OPENER.open(req, timeout=300) as resp, tmp.open("wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"下载文件超过安全上限（>{max_bytes // (1024 * 1024)}MB），已中止")
                h.update(chunk)
                fh.write(chunk)
        digest = h.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise RuntimeError("SHA-256 校验失败，文件可能已损坏或被篡改")
        os.replace(tmp, dest)
        return total, digest
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def upload_file_sync(url: str, src: Path, max_bytes: int = MAX_TRANSFER_BYTES) -> tuple[int, str]:
    """Stream-upload a local file to Riesz Manager with max size check, no redirects, and SHA-256."""
    if not src.is_file():
        raise RuntimeError(f"本地文件不存在：{src}")
    size = src.stat().st_size
    if size > max_bytes:
        raise RuntimeError(f"本地文件超过上传上限（>{max_bytes // (1024 * 1024)}MB）")
    h = hashlib.sha256()
    total = 0
    with src.open("rb") as fh:
        while chunk := fh.read(256 * 1024):
            total += len(chunk)
            h.update(chunk)
    digest = h.hexdigest()

    def _body_iter():
        with src.open("rb") as fh:
            while chunk := fh.read(256 * 1024):
                yield chunk

    req = urllib.request.Request(
        url,
        data=_body_iter(),
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(total),
            "X-Riesz-SHA256": digest,
            "User-Agent": "riesz-node/0.1",
        },
    )
    with _OPENER.open(req, timeout=300) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"上传失败：HTTP {resp.status}")
    return total, digest
