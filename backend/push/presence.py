"""Which session each connected browser is showing, and whether someone is looking (in memory).

Browsers report over their WebSocket the session on screen and whether the page
is in active use (visible, focused, touched recently), refreshing it every 30
seconds. A notification about a session that someone is looking at is not sent.
"""

from __future__ import annotations

import time

ACTIVE_FOR = 75  # seconds a report stays valid without a refresh

_viewers: dict[str, tuple[str | None, bool, float]] = {}


def report(channel: str, session_id: str | None, active: bool) -> None:
    _viewers[channel] = (session_id, active, time.time())


def forget(channel: str) -> None:
    _viewers.pop(channel, None)


def is_watched(session_id: str) -> bool:
    now = time.time()
    return any(sid == session_id and active and now - ts < ACTIVE_FOR for sid, active, ts in _viewers.values())
