# riesz-node

Cross-platform reverse-connected local computer node client for **Riesz Manager**.

Supports Windows (PowerShell / cmd), WSL, macOS, and Linux.

## Zero-Config One-Liner (No git, no manual install)

Run on any machine with Python 3.10+ (auto-creates a cached venv, auto-updates when the server updates, and interactively asks for your pairing code on first run):

```bash
python -c "import urllib.request; exec(urllib.request.urlopen('https://your-riesz-host/api/nodes/bootstrap.py').read())"
```

Or with `uv`:

```bash
uvx --from https://your-riesz-host/api/nodes/client.zip riesz-node connect --server https://your-riesz-host
```

## Per-Session Directory Isolation

You do **not** have to choose `--dir` at startup. When a session requests to bind to your computer, the TUI lets you pick or browse (`浏览目录…`) the exact directory (or directories) allowed for **that specific session**. Different sessions are strictly isolated to their own granted directories, and you can press `e` at any time to edit a bound session's directories.

## Shortcuts in TUI

- `y` / `Ctrl+Y`: approve bind request (authorizing the entered/picked directories) or pending command
- `n`: reject bind request or pending command
- `e`: edit allowed directories of the selected bound session
- `k`: kill the currently selected running command
- `u`: unbind the currently selected session
- `Ctrl+Q`: kill running commands, disconnect, and exit
