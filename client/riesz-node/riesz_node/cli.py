"""CLI entry point for `riesz-node` (ephemeral, per-session pairing)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .agent import MODES, NodeAgent, pair_session
from .policy import PolicyError, normalize_roots


def _ask_pairing_code(server: str) -> str:
    if not sys.stdin.isatty():
        raise SystemExit("请通过 --code RZ-XXXX-XXXX 传入对应会话生成的一次性配对码。")
    print(f"\nRiesz Manager 服务器：{server}")
    print("请在需要绑定的会话内点击「本地电脑」→「生成一次性配对码」，粘贴到下方：")
    try:
        code = input("配对码 (RZ-XXXX-XXXX，直接回车退出): ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(1) from None
    if not code:
        raise SystemExit("已取消。")
    return code


async def _run_plain(agent: NodeAgent) -> None:
    dirs_txt = "; ".join(str(p) for p in agent.dirs) if agent.dirs else "（未授权目录，plain 模式请传 --dir）"
    print(
        f"riesz-node (plain) | 名称: {agent.name} | 绑定会话: 「{agent.session_title}」({agent.session_id[:8]})"
        f" | 权限: {agent.mode} | 允许目录: {dirs_txt}",
        flush=True,
    )
    try:
        await agent.run()
    except KeyboardInterrupt:
        await agent.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="riesz-node", description="Riesz Manager 跨平台本地受控节点客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_conn = sub.add_parser("connect", help="使用会话配对码建立一次性加密连接（不落盘私钥）")
    p_conn.add_argument("--server", required=True, help="Riesz Manager 服务器地址，例如 https://example.com")
    p_conn.add_argument("--code", default="", help="会话内生成的一次性配对码（不传则交互输入）")
    p_conn.add_argument("--name", default="", help="本客户端显示名称（不传则复用上次自定义名或自动分配）")
    p_conn.add_argument("--dir", dest="dirs", action="append", default=[],
                        help="可选：本次授权的本地目录（可多次传；不传时自动恢复该会话上次授权目录或在 TUI 选择）")
    p_conn.add_argument("--mode", choices=MODES, default="full",
                        help="本机允许的最高权限档位上限（默认 full，实际权限以会话网页端设定为准）")
    p_conn.add_argument("--note", default="", help="展示给 Agent 的环境备注（如 GPU / Windows 环境）")
    p_conn.add_argument("--plain", action="store_true", help="纯控制台滚动日志模式（不启动全屏 TUI）")

    args = parser.parse_args(argv)

    if args.cmd == "connect":
        try:
            cli_dirs: list[Path] = normalize_roots(args.dirs, allow_empty=True)
        except PolicyError as exc:
            raise SystemExit(f"错误：{exc}") from None

        code = args.code.strip() or _ask_pairing_code(args.server)
        try:
            session = pair_session(args.server, code, name=args.name, cli_dirs=cli_dirs or None)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"错误：{exc}") from None

        if args.plain and not session["dirs"]:
            raise SystemExit("错误：--plain 模式下该会话尚无历史目录记录，请通过 --dir 指定至少一个允许目录。")

        agent = NodeAgent(
            session=session,
            max_mode=args.mode,
            note=args.note,
            interactive=not args.plain,
            on_log=(lambda m: print(m, flush=True)) if args.plain else None,
        )
        if args.plain:
            asyncio.run(_run_plain(agent))
            return

        from .tui import NodeTUI
        app = NodeTUI(agent)
        app.run()


if __name__ == "__main__":
    main()
