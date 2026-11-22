"""CLI entry point for `riesz-node`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .agent import MODES, NodeAgent, load_devices, normalize_server_url, pair_device
from .policy import PolicyError, normalize_roots


def _pick_device(selector: str | None, server: str | None) -> dict:
    devices = load_devices()
    if server:
        norm = normalize_server_url(server)
        devices = [d for d in devices if normalize_server_url(str(d.get("server") or "")) == norm]
    if selector:
        sel = selector.strip().lower()
        for d in devices:
            if str(d.get("nodeId") or "").lower().startswith(sel) or str(d.get("name") or "").lower() == sel:
                return d
        raise SystemExit(f"找不到已配对的节点：{selector}（请先运行 riesz-node pair）")
    if not devices:
        raise SystemExit("尚未配对任何节点。请先在 Riesz Manager 网页生成配对码，然后运行：\n"
                         "  riesz-node pair --server https://<你的地址> --code RZ-XXXX-XXXX")
    return devices[0]


async def _run_plain(agent: NodeAgent) -> None:
    print(f"riesz-node (plain) | 节点: {agent.name} | 权限上限: {agent.max_mode} | 目录: {', '.join(str(r) for r in agent.roots)}", flush=True)
    if not agent.auto_accept_bind:
        print("提示：plain 模式无法本地按键审批绑定，如需自动接受网页端绑定请加 --auto-accept-bind；命令审批可在网页端点击确认。", flush=True)
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
    p_conn.add_argument("--dir", dest="dirs", action="append", required=True,
                        help="允许操作的本地目录白名单（可多次传 --dir）")
    p_conn.add_argument("--mode", choices=MODES, default="auto",
                        help="本机允许的最高权限档位（默认 auto）")
    p_conn.add_argument("--note", default="", help="展示给主 Agent 的能力备注（如 GPU / Windows 测试环境）")
    p_conn.add_argument("--server", default="", help="服务器地址（首次用 --code 配对或区分多台服务器时填写）")
    p_conn.add_argument("--code", default="", help="可选：一次性配对码（未配对时自动完成配对后直接连接）")
    p_conn.add_argument("--name", default="", help="配合 --code 使用的节点名称")
    p_conn.add_argument("--node", default="", help="选择已配对的节点名或 ID 前缀")
    p_conn.add_argument("--ephemeral", action="store_true", help="临时模式：配合 --code，不落盘密钥，断开即作废")
    p_conn.add_argument("--auto-accept-bind", action="store_true", help="自动接受来自网页端的会话绑定请求")
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
        print(f"密钥已安全保存（仅本机可读）。现在可以运行：")
        print(f"  riesz-node connect --dir <你的工作目录> --mode auto")
        return

    if args.cmd == "connect":
        try:
            roots: list[Path] = normalize_roots(args.dirs)
        except PolicyError as exc:
            raise SystemExit(f"错误：{exc}") from None

        if args.code:
            if not args.server:
                raise SystemExit("使用 --code 配对连接时必须同时指定 --server")
            device = pair_device(args.server, args.code, name=args.name, ephemeral=args.ephemeral)
        else:
            device = _pick_device(args.node or None, args.server or None)

        agent = NodeAgent(
            device=device,
            roots=roots,
            max_mode=args.mode,
            note=args.note,
            auto_accept_bind=bool(args.auto_accept_bind),
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
