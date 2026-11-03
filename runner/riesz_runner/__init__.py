"""RieszManager host runner.

A small host-side daemon that owns everything which must happen *on the host*
rather than inside a container: reading Claude Code's data directory, browsing
the workspace, and spawning/driving `claude` processes in the user's real
environment. It exposes an HTTP + WebSocket API on a Unix domain socket that
the (containerised) Django backend talks to.
"""

__version__ = "0.1.0"
