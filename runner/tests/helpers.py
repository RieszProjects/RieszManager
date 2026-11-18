"""Shared test helpers: fixture loading, an event-collecting sink, a scripted JSON-RPC peer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_traffic(name: str) -> list[dict[str, Any]]:
    """Recorded traffic: [{"t", "dir": "in"|"out"|"mark", "msg"}]."""
    return [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def responses_by_method(traffic: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Results the harness gave, per request method, in order."""
    methods: dict[Any, str] = {}
    out: dict[str, list[Any]] = {}
    for rec in traffic:
        msg = rec["msg"]
        if not isinstance(msg, dict):
            continue
        if rec["dir"] == "out" and "method" in msg and "id" in msg:
            methods[msg["id"]] = msg["method"]
        elif rec["dir"] == "in" and "id" in msg and "method" not in msg and msg["id"] in methods:
            out.setdefault(methods[msg["id"]], []).append(msg.get("result"))
    return out


class Events:
    """A sink that keeps every event a driver emits."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.on_permission = None          # callable(driver, request) -> None, to answer prompts

    async def __call__(self, driver: Any, event: dict[str, Any]) -> None:
        self.events.append(event)
        if event.get("ev") == "permission" and self.on_permission:
            self.on_permission(driver, event["request"])

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("ev") == kind]


class FakePeer:
    """Answers requests from a script: {method: result | callable(params) -> result | Exception}."""

    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.script = script or {}
        self.requests: list[tuple[str, Any]] = []
        self.notifications: list[tuple[str, Any]] = []
        self.stopped = False
        self.pid = 4242
        self.stderr_tail: list[str] = []

    async def request(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        self.requests.append((method, params))
        answer = self.script.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    async def notify(self, method: str, params: Any = None) -> None:
        self.notifications.append((method, params))

    async def stop(self) -> None:
        self.stopped = True

    @property
    def alive(self) -> bool:
        return not self.stopped

    def sent(self, method: str) -> list[Any]:
        return [p for m, p in self.requests if m == method]
