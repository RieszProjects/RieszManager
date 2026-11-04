"""Commands a claude process is still running, read from the process tree.

Claude Code runs every Bash tool command (including background commands and the
ones Monitor watches) in a child shell that sources a snapshot from
~/.claude/shell-snapshots. While such a child exists, ending the claude process
would kill that command, whatever the session's status says.
"""

from __future__ import annotations

import os

MARKER = b"shell-snapshots"


def running_commands(pid: int | None) -> int:
    """How many Bash tool commands the claude process `pid` has running right now."""
    if not pid:
        return 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0
    count = 0
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read()
            # "pid (comm) state ppid ...": the comm may contain spaces, so split after ")".
            if int(stat[stat.rindex(b")") + 2:].split()[1]) != pid:
                continue
            with open(f"/proc/{name}/cmdline", "rb") as f:
                if MARKER in f.read():
                    count += 1
        except (OSError, ValueError, IndexError):
            continue
    return count
