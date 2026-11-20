"""The stdio MCP server through which every harness gets Riesz Manager's tools (tools.py).

A harness starts it as one of its MCP servers (see tools.server()); it answers
the MCP handshake and relays tools/list and tools/call to the runner over the
runner's Unix socket. The runner works out from the connection which session's
harness started it. Standard library only, run with `python -I`: it must not
import anything from the directory of whatever project the harness runs in.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import threading
from typing import Any

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")   # newest first
EMPTY_LISTS = {"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}
RUNNER_DOWN = "Riesz Manager 的 runner 没有响应（可能正在重启），过一会儿再试"


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("runner", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class Relay:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.write_lock = threading.Lock()

    def runner(self, method: str, path: str, body: Any = None, timeout: float = 200) -> Any:
        conn = _Unix(self.socket_path, timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            res = conn.getresponse()
            payload = json.loads(res.read() or b"{}")
        finally:
            conn.close()
        if res.status >= 400:
            raise RuntimeError(payload.get("error") if isinstance(payload, dict) else f"HTTP {res.status}")
        return payload

    def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self.write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def answer(self, req: dict[str, Any]) -> None:
        method = req.get("method")
        params = req.get("params") or {}
        try:
            result = self.handle(method, params)
        except LookupError:
            self.send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": f"method not found: {method}"}})
            return
        except Exception as exc:  # noqa: BLE001 - reported to the harness
            self.send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": str(exc) or RUNNER_DOWN}})
            return
        self.send({"jsonrpc": "2.0", "id": req["id"], "result": result})

    def handle(self, method: str | None, params: dict[str, Any]) -> Any:
        if method == "initialize":
            asked = params.get("protocolVersion")
            try:
                listing = self.runner("GET", "/v1/tools", timeout=20)
            except Exception:  # noqa: BLE001 - the tools are listed again on tools/list
                listing = {}
            return {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": listing.get("name") or "riesz", "title": "Riesz Manager", "version": "1"},
                    **({"instructions": listing["instructions"]} if listing.get("instructions") else {})}
        if method == "ping":
            return {}
        if method == "tools/list":
            try:
                return {"tools": self.runner("GET", "/v1/tools", timeout=20)["tools"]}
            except (OSError, ValueError) as exc:
                raise RuntimeError(RUNNER_DOWN) from exc
        if method == "tools/call":
            try:
                return self.runner("POST", "/v1/tools/call", {"name": params.get("name"), "arguments": params.get("arguments") or {}})
            except (OSError, ValueError):
                return {"content": [{"type": "text", "text": RUNNER_DOWN}], "isError": True}
            except RuntimeError as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        if method in EMPTY_LISTS:                               # some clients ask whatever the capabilities say
            return {EMPTY_LISTS[method]: []}
        raise LookupError(method)

    def serve(self) -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if not isinstance(req, dict) or "id" not in req or "method" not in req:
                continue                                        # notifications and responses: nothing to answer
            if req["method"] == "tools/call":
                threading.Thread(target=self.answer, args=(req,), daemon=True).start()
            else:
                self.answer(req)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", required=True, help="the runner's Unix socket")
    Relay(parser.parse_args().socket).serve()


if __name__ == "__main__":
    main()
