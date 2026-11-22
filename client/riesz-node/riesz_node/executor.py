"""Cross-platform async command execution and streaming file transfer."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


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
            return [pwsh, "-NoProfile", "-NonInteractive", "-Command", command]
        return ["cmd.exe", "/c", command]
    bash = shutil.which("bash")
    if bash:
        return [bash, "-lc", command]
    return ["/bin/sh", "-c", command]


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


def download_file_sync(url: str, dest: Path, expected_sha256: str | None = None) -> tuple[int, str]:
    """Stream-download a file from Riesz Manager into `dest`, verifying SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.rz.tmp")
    h = hashlib.sha256()
    total = 0
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "riesz-node/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
                fh.write(chunk)
        digest = h.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise RuntimeError("SHA-256 校验失败，文件可能已损坏")
        os.replace(tmp, dest)
        return total, digest
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def upload_file_sync(url: str, src: Path) -> tuple[int, str]:
    """Stream-upload a local file to Riesz Manager with SHA-256 verification."""
    if not src.is_file():
        raise RuntimeError(f"本地文件不存在：{src}")
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
    with urllib.request.urlopen(req, timeout=300) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"上传失败：HTTP {resp.status}")
    return total, digest
