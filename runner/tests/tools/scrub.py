"""Turn raw recordings (tests/tools/record.py) into test fixtures that are safe to publish.

    python -m tests.tools.scrub codex RAW.jsonl OUT.jsonl
    python -m tests.tools.scrub dsh RAW.jsonl OUT.jsonl
    python -m tests.tools.scrub dsh-log SESSION.jsonl[.zstd] OUT.jsonl

Removes what identifies the machine or account (host names, installation ids,
home directories, user agents) and replaces bulky harness internals (dsh's
system prompt and request headers) with short placeholders.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any

HOME = os.path.expanduser("~")
DROP_METHODS = {"remoteControl/status/changed"}       # host name and installation id
PLACEHOLDER = "<removed from the fixture>"


def _strings(value: Any) -> Any:
    if isinstance(value, str):
        value = value.replace(HOME, "/home/user")
        return re.sub(r"ip-\d+-\d+-\d+-\d+", "host", value)
    if isinstance(value, list):
        return [_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _strings(v) for k, v in value.items()}
    return value


def _dsh_event(ev: dict[str, Any]) -> dict[str, Any]:
    kind = ev.get("type")
    if kind in ("request/header",):
        ev = {**ev, "data": {"header": PLACEHOLDER, "reason": (ev.get("data") or {}).get("reason")}}
    elif kind == "system/message":
        data = dict(ev.get("data") or {})
        message = dict(data.get("message") or {})
        message["content"] = [{"type": "text", "text": PLACEHOLDER}]
        data["message"] = message
        ev = {**ev, "data": data}
    elif kind == "assistant/message":
        data = dict(ev.get("data") or {})
        data["stream"] = []                              # raw provider stream records: bulky and not used
        ev = {**ev, "data": data}
    return ev


def scrub_traffic(which: str, src: str, dst: str) -> None:
    with open(src, encoding="utf-8") as fin, open(dst, "w", encoding="utf-8") as fout:
        for line in fin:
            rec = json.loads(line)
            msg = rec["msg"]
            if isinstance(msg, dict):
                if msg.get("method") in DROP_METHODS:
                    continue
                result = msg.get("result")
                if isinstance(result, dict) and "userAgent" in result:
                    msg = {**msg, "result": {**result, "userAgent": "riesz-manager/0 (Linux; x86_64)"}}
                if which == "dsh" and msg.get("method") == "session.event":
                    params = dict(msg["params"])
                    params["event"] = _dsh_event(params["event"])
                    msg = {**msg, "params": params}
            rec["msg"] = _strings(msg)
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")


def scrub_dsh_log(src: str, dst: str) -> None:
    raw = subprocess.run(["zstd", "-dc", src], capture_output=True, check=True).stdout if src.endswith(".zstd") else open(src, "rb").read()
    with open(dst, "w", encoding="utf-8") as fout:
        for line in raw.decode().splitlines():
            if not line.strip():
                continue
            rec = _strings(_dsh_event(json.loads(line)))
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    which, src, dst = sys.argv[1:4]
    if which == "dsh-log":
        scrub_dsh_log(src, dst)
    else:
        scrub_traffic(which, src, dst)


if __name__ == "__main__":
    main()
