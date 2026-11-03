"""Entry point: `python -m riesz_runner [--env-file PATH]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from aiohttp import web

from .config import load_config
from .server import build_app


async def _serve(env_file: str | None) -> None:
    cfg = load_config(env_file)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.state_dir, 0o700)
    cfg.socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.socket_path.parent, 0o750)
    if cfg.socket_path.exists():
        cfg.socket_path.unlink()

    app = build_app(cfg)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.UnixSite(runner, str(cfg.socket_path))
    await site.start()
    os.chmod(cfg.socket_path, 0o660)
    logging.getLogger("riesz_runner").info(
        "runner listening on %s (workspace %s, claude %s)", cfg.socket_path, cfg.workspace_root, cfg.claude_bin
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(prog="riesz_runner", description="RieszManager host runner")
    parser.add_argument("--env-file", default=os.environ.get("RIESZ_ENV_FILE"), help="path to the .env file")
    parser.add_argument("--log-level", default=os.environ.get("RUNNER_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_serve(args.env_file))


if __name__ == "__main__":
    main()
