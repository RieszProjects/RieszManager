# riesz-node

Cross-platform reverse-connected local computer node client for **Riesz Manager**.

Supports Windows (PowerShell / cmd), WSL, macOS, and Linux.

## Install

```bash
pip install ./client/riesz-node
# or with uv:
uv tool install ./client/riesz-node
```

## Quick Start

1. Open Riesz Manager in your browser, click **本地节点** in the top bar, and generate a one-time pairing code (`RZ-XXXX-XXXX`).
2. Pair and connect on your computer:

```bash
# Permanent Ed25519 key pairing (stored locally with 0600 permissions)
riesz-node pair --server https://your-riesz-host --code RZ-XXXX-XXXX --name win-pc
riesz-node connect --dir D:\Sandbox --mode auto --note "Windows 11 + GPU"

# Or one-shot ephemeral mode (key stays in RAM only, deleted on disconnect)
riesz-node connect --server https://your-riesz-host --code RZ-XXXX-XXXX --ephemeral --dir ~/sandbox --mode auto
```

## Shortcuts in TUI

- `y` / `n`: approve or reject a session bind request or pending command approval (also synced with the Riesz Manager web UI)
- `k`: kill the currently selected running command
- `u`: unbind the currently selected session
- `Ctrl+Q`: kill running commands, disconnect, and exit

Pass `--plain` for a non-fullscreen scrolling log output mode.
