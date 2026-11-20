"""Permission modes across harnesses. Every mode belongs to one tier (Mode.tier), and every harness
says which of its modes to use for each tier (HarnessSpec.modes): a session moving to another harness
takes the mode that harness names for the tier of the mode it had.

Tiers, from most to least careful:
- readonly: reads and plans only;
- ask:      asks before editing files and before running commands;
- edits:    edits files without asking, still asks for commands;
- auto:     works on its own within limits (a sandbox, a classifier), asks beyond them;
- full:     never asks.
"""

from __future__ import annotations

TIERS = ("readonly", "ask", "edits", "auto", "full")

# Claude Code's modes (it is not run through a driver, so its tiers live here).
CLAUDE_TIERS = {"plan": "readonly", "default": "ask", "acceptEdits": "edits", "auto": "auto", "bypassPermissions": "full"}


# Which Claude Code mode stands for each tier (every harness fills in the same table: HarnessSpec.modes).
CLAUDE_MODE_TABLE = {"readonly": "plan", "ask": "default", "edits": "acceptEdits", "auto": "auto", "full": "bypassPermissions"}
