"""A tiny JSON-RPC server over stdio for testing JsonRpcPeer.

Methods: echo (returns params), fail (an error), ask (asks the client "confirm"
and returns its answer), notify (sends a "tick" notification), spawn (starts a
long `sleep` child and returns its pid), big (returns a 3 MB string), quit
(exits with code 3). Lines that are not JSON are written to stdout as noise first.
"""

import json
import subprocess
import sys

print("starting up (noise that is not JSON)", flush=True)
pending = {}
next_id = 1000


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    if "method" not in msg:                       # an answer to our "confirm"
        rid = pending.pop(msg["id"])
        send({"id": rid, "result": msg.get("result", msg.get("error"))})
        continue
    method, rid, params = msg["method"], msg.get("id"), msg.get("params")
    if method == "echo":
        send({"id": rid, "result": params})
    elif method == "fail":
        send({"id": rid, "error": {"code": -32001, "message": "it failed", "data": {"why": "asked to"}}})
    elif method == "ask":
        next_id += 1
        pending[next_id] = rid
        send({"id": next_id, "method": "confirm", "params": params})
    elif method == "notify":
        send({"method": "tick", "params": {"n": 1}})
        send({"id": rid, "result": "sent"})
    elif method == "spawn":
        child = subprocess.Popen(["sleep", "300"])
        send({"id": rid, "result": child.pid})
    elif method == "big":
        send({"id": rid, "result": "x" * 3_000_000})
    elif method == "quit":
        sys.exit(3)
