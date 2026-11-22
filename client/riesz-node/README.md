# riesz-node

Cross-platform ephemeral, session-scoped local computer node client for **Riesz Manager**.

Supports Windows (PowerShell / cmd), WSL, macOS, and Linux.

## Security & Session Isolation

- **Zero persistent keys on disk**: every launch generates a fresh in-memory Ed25519 keypair and pairs directly to **one specific session** using a short-lived, single-use pairing code.
- **Strictly session-scoped**: only the paired session can see or invoke this client. Closing the client destroys the connection and drops all server state immediately.
- **Local convenience memory (`prefs.json`)**: stores only non-sensitive per-session preferences (previously authorized directories and custom display name) so re-binding a session you have used before automatically restores its directories and name (with automatic conflict suffixes if another client in the same session uses the same name).

## Zero-Config One-Liner

Copy the one-liner from inside any Riesz Manager session ("本地电脑" dialog, protected by your private download token):

```bash
python -c "import urllib.request as u; exec(u.urlopen(u.Request('https://your-riesz-host/api/nodes/bootstrap.py?token=<YOUR_TOKEN>', headers={'User-Agent': 'riesz-node/1'})).read())"
```

## Shortcuts in TUI

- `e`: add/remove multiple allowed directories (with folder browser, parent navigation & Windows drive switcher)
- `r`: rename this client process (synced to web UI and remembered for this session)
- `y` / `n`: approve or reject pending command / local high-risk confirmation
- `k`: kill the currently selected running command
- `Ctrl+Q`: kill running commands, disconnect, and exit
