"""CLI entry point for `riesz-node` (supports zero-config interactive first-time pairing)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .agent import MODES, NodeAgent, load_devices, normalize_server_url, pair_device
from .policy import PolicyError, normalize_roots


def _find_device(selector: str | None, server: str | None) -> dict | None:
    devices = load_devices()
    if server:
        norm = normalize_server_url(server)
        devices = [d for d in devices if normalize_server_url(str(d.get("server") or "")) == norm]
    if selector:
        sel = selector.strip().lower()
        for d in devices:
            if str(d.get("nodeId") or "").lower().startswith(sel) or str(d.get("name") or "").lower() == sel:
                return d
        return None
    return devices[0] if devices else None


def _interactive_pair(server: str, *, name: str = "", ephemeral: bool = False) -> dict:
    if not sys.stdin.isatty():
        raise SystemExit("本机尚未配对该服务器。请传 --code RZ-XXXX-XXXX 或先运行 riesz-node pair。")
    print(f"\n本机尚未配对服务器：{server}")
    print("请在 Riesz Manager 网页顶栏点击「本地节点」→「生成配对码」，把配对码粘贴到下面：")
    try:
        code = input("配对码 (RZ-XXXX-XXXX，直接回车取消): ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(1) from None
    if not code:
        raise SystemExit("已取消。")
    if not ephemeral:
        try:
            ans = input("是否使用临时模式（不保存私钥到本地，断开即删）？[y/N]: ").strip().lower()
            ephemeral = ans in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            pass
    entry = pair_device(server, code, name=name, ephemeral=ephemeral)
    print(f"配对成功！节点：{entry['name']}（{'临时模式' if entry.get('ephemeral') else '已保存本机 Ed25519 密钥'}）\n")
    return entry


async def _run_plain(agent: NodeAgent) -> None:
    dirs_txt = ", ".join(str(r) for r in agent.roots) if agent.roots else "（无预设，需 TUI 选择）"
    print(f"riesz-node (plain) | 节点: {agent.name} | 权限上限: {agent.max_mode} | 默认目录: {dirs_txt}", flush=True)
    if not agent.auto_accept_bind:
        print("提示：plain 模式无法本地交互选目录/审批绑定，如需自动接受请同时传 --dir 与 --auto-accept-bind。", flush=True)
    try:
        await agent.run()
    except KeyboardInterrupt:
        await agent.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="riesz-node", description="Riesz Manager 跨平台本地受控节点客户端")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pair = sub.add_parser("pair", help="使用一次性配对码与 Riesz Manager 配对并生成本机 Ed25519 密钥")
    p_pair.add_argument("--server", required=True, help="Riesz Manager 地址，例如 https://example.com")
    p_pair.add_argument("--code", required=True, help="网页端生成的一次性配对码（RZ-XXXX-XXXX）")
    p_pair.add_argument("--name", default="", help="给这台电脑起的名字（默认主机名）")

    p_conn = sub.add_parser("connect", help="主动连接 Riesz Manager 并启动本地受控节点面板")
    p_conn.add_argument("--dir", dest="dirs", action="append", default=[],
                        help="可选：预设默认允许目录（可多次传；不传时在 TUI 里按会话单独选目录）")
    p_conn.add_argument("--mode", choices=MODES, default="auto",
                        help="本机允许的最高权限档位（默认 auto）")
    p_conn.add_argument("--note", default="", help="展示给主 Agent 的能力备注（如 GPU / Windows 测试环境）")
    p_conn.add_argument("--server", default="", help="服务器地址（未配对时自动引导配对，或区分多台服务器）")
    p_conn.add_argument("--code", default="", help="可选：一次性配对码（未配对时自动完成配对后直接连接）")
    p_conn.add_argument("--name", default="", help="配合 --code / 首次配对使用的节点名称")
    p_conn.add_argument("--node", default="", help="选择已配对的节点名或 ID 前缀")
    p_conn.add_argument("--ephemeral", action="store_true", help="临时模式：不落盘密钥，断开即作废")
    p_conn.add_argument("--auto-accept-bind", action="store_true", help="自动接受绑定（仅在传了 --dir 时生效）")
    p_conn.add_argument("--plain", action="store_true", help="纯控制台滚动日志模式（不启动全屏 TUI）")

    sub.add_parser("devices", help="查看本机已配对的节点列表")

    args = parser.parse_args(argv)

    if args.cmd == "devices":
        devs = load_devices()
        if not devs:
            print("暂无已配对节点。")
            return
        for d in devs:
            print(f"- {d['name']}  id={d['nodeId']}  server={d['server']}")
        return

    if args.cmd == "pair":
        entry = pair_device(args.server, args.code, name=args.name)
        print(f"配对成功！节点名称：{entry['name']}  ID：{entry['nodeId']}")
        print("现在可以运行：riesz-node connect")
        return

    if args.cmd == "connect":
        try:
            roots: list[Path] = normalize_roots(args.dirs, allow_empty=True)
        except PolicyError as exc:
            raise SystemExit(f"错误：{exc}") from None

        if args.plain and args.auto_accept_bind and not roots:
            raise SystemExit("错误：--plain --auto-accept-bind 模式下必须传至少一个 --dir")

        if args.code:
            if not args.server:
                raise SystemExit("使用 --code 配对连接时必须同时指定 --server")
            device = pair_device(args.server, args.code, name=args.name, ephemeral=args.ephemeral)
        else:
            device = _find_device(args.node or None, args.server or None)
            if device is None:
                if args.node:
                    raise SystemExit(f"找不到已配对的节点：{args.node}")
                if not args.server:
                    raise SystemExit("尚未配对任何节点，请传 --server https://<你的地址> 以自动引导配对。")
                device = _interactive_pair(normalize_server_url(args.server), name=args.name, ephemeral=args.ephemeral)

        agent = NodeAgent(
            device=device,
            roots=roots,
            max_mode=args.mode,
            note=args.note,
            auto_accept_bind=bool(args.auto_accept_bind),
            interactive=not bool(args.plain),
            on_log=(lambda line: print(line, flush=True)) if args.plain else None,
        )
        if args.plain:
            try:
                asyncio.run(_run_plain(agent))
            except KeyboardInterrupt:
                pass
        else:
            from .tui import NodeTUI
            NodeTUI(agent).run()


if __name__ == "__main__":
    main()
