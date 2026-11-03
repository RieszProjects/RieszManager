"""Entry point: `python -m riesz_runner [--env-file PATH]`.

SIGTERM/SIGINT stop the runner. SIGHUP requests a graceful reload: the runner
waits until none of its claude sessions is working, stops the idle ones (they
resume transparently on the next message) and re-executes itself with the
current code on disk.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from aiohttp import web

from .config import load_config
from .server import HUB, build_app

log = logging.getLogger("riesz_runner")


async def _serve(env_file: str | None) -> bool:
    """Run until stopped. Returns True when the process should re-execute itself."""
    cfg = load_config(env_file)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.state_dir, 0o700)
    cfg.socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.socket_path.parent, 0o750)
    if cfg.socket_path.exists():
        cfg.socket_path.unlink()

    app = build_app(cfg)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
    await runner.setup()
    site = web.UnixSite(runner, str(cfg.socket_path))
    await site.start()
    os.chmod(cfg.socket_path, 0o660)
    logging.getLogger("riesz_runner").info(
        "runner listening on %s (workspace %s, claude %s)", cfg.socket_path, cfg.workspace_root, cfg.claude_bin
    )

    hub = app[HUB]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    loop.add_signal_handler(signal.SIGHUP, hub.request_reload)
    stop_task = asyncio.create_task(stop.wait())
    reload_task = asyncio.create_task(hub.reload_ready.wait())
    await asyncio.wait({stop_task, reload_task}, return_when=asyncio.FIRST_COMPLETED)
    reloading = reload_task.done() and not stop_task.done()
    stop_task.cancel()
    reload_task.cancel()
    await runner.cleanup()
    return reloading


def main() -> None:
    parser = argparse.ArgumentParser(prog="riesz_runner", description="RieszManager host runner")
    parser.add_argument("--env-file", default=os.environ.get("RIESZ_ENV_FILE"), help="path to the .env file")
    parser.add_argument("--log-level", default=os.environ.get("RUNNER_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if asyncio.run(_serve(args.env_file)):
        log.info("re-executing runner with updated code")
        os.execv(sys.executable, [sys.executable, "-m", "riesz_runner", *sys.argv[1:]])


if __name__ == "__main__":
    main()
