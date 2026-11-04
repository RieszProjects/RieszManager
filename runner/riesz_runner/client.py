"""`rieszctl session ...`: start and drive hosted sessions from the command line.

Meant for other Claude Code sessions on the same machine, so they can open a
session that RieszManager hosts exactly like one opened in the web UI (Claude in
Chrome, cross-session messages, permission prompts in the web, keep running,
push notifications), instead of a plain `claude` process that RieszManager can
only watch.

It talks to the runner's Unix socket with the token from .env, so the token is
never passed on a command line or printed. Standard library only: it runs with
any python3, without the runner's virtualenv.

    rieszctl session new [-C DIR] [MESSAGE]   start a session (MESSAGE or stdin)
    rieszctl session send ID [MESSAGE]        send it a message
    rieszctl session wait ID                  wait until it is idle, print its last reply
    rieszctl session reply ID                 print its last reply now
    rieszctl session show ID                  status
    rieszctl session list [--mine] [--all]    running sessions
    rieszctl session keep ID on|off           keep running (not stopped when idle)
    rieszctl session stop ID                  stop its claude process

IDs may be shortened to a unique prefix (at least 6 characters).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

from .config import load_env_file, runner_socket

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MIN_PREFIX = 6
POLL_SECONDS = 2.0
MODES = ["default", "acceptEdits", "plan", "auto", "bypassPermissions", "dontAsk", "manual"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


class CliError(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("runner", timeout=timeout)
        self.socket_path = str(path)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class Runner:
    def __init__(self, env_file: Path) -> None:
        env = load_env_file(env_file)
        env.update({k: v for k, v in os.environ.items() if k.startswith("RUNNER_")})
        self.token = env.get("RUNNER_TOKEN", "")
        self.socket = runner_socket(env)
        if not self.token:
            raise CliError(f"RUNNER_TOKEN is not set in {env_file}")

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 120) -> Any:
        conn = _UnixConnection(self.socket, timeout)
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            res = conn.getresponse()
            raw = res.read()
        except (FileNotFoundError, ConnectionRefusedError):
            raise CliError(f"the RieszManager runner is not running (no socket at {self.socket})") from None
        except OSError as exc:
            raise CliError(f"cannot reach the runner: {exc}") from None
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"error": raw.decode(errors="replace").strip()}
        if res.status >= 400:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise CliError(message or f"runner answered HTTP {res.status}")
        return payload


# ---- helpers -----------------------------------------------------------------

def own_session() -> str | None:
    """The calling Claude Code session, when run from one (its Bash tool sets this)."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return sid if UUID_RE.match(sid) else None


def read_message(words: list[str], required: bool = True) -> str:
    if words and words != ["-"]:
        return " ".join(words)
    if words == ["-"] or not sys.stdin.isatty():
        text = sys.stdin.read()
        if text.strip():
            return text
    if required:
        raise CliError("no message: pass it as an argument or on standard input")
    return ""


def all_sessions(runner: Runner) -> list[dict[str, Any]]:
    return runner.call("GET", "/v1/sessions?limit=2000").get("sessions", [])


def resolve(runner: Runner, ident: str) -> str:
    ident = ident.strip().lower()
    if UUID_RE.match(ident):
        return ident
    if len(ident) < MIN_PREFIX or not re.fullmatch(r"[0-9a-f-]+", ident):
        raise CliError(f"not a session id: {ident!r} (give at least {MIN_PREFIX} characters of it)")
    found = [s["sessionId"] for s in all_sessions(runner) if s.get("sessionId", "").startswith(ident)]
    if not found:
        raise CliError(f"no session starts with {ident}")
    if len(found) > 1:
        raise CliError(f"{ident} is ambiguous: " + ", ".join(found[:5]))
    return found[0]


def not_self(sid: str, action: str) -> None:
    if sid == own_session():
        raise CliError(f"refusing to {action} the session this command runs in")


def title_of(summary: dict[str, Any]) -> str:
    return summary.get("title") or summary.get("customTitle") or summary.get("firstPrompt") or ""


def state_word(state: dict[str, Any]) -> str:
    if state.get("permissions"):
        return "waiting"   # for a permission decision in the web UI
    if state.get("busy") or state.get("queue"):
        return "working"
    if state.get("alive"):
        return "idle"
    return "stopped"


def last_reply(runner: Runner, sid: str) -> str:
    """Text of the session's latest assistant message that has any."""
    items = runner.call("GET", f"/v1/sessions/{sid}/items?limit=200").get("items", [])
    for item in reversed(items):
        if item.get("kind") != "assistant":
            continue
        texts = [b.get("text", "") for b in item.get("blocks") or [] if b.get("type") == "text" and b.get("text")]
        if texts:
            return "\n\n".join(texts).strip()
    return ""


def wait_idle(runner: Runner, sid: str, timeout: float) -> dict[str, Any]:
    """Poll until the session has finished its turn and has nothing queued (seen twice in a row)."""
    deadline = time.monotonic() + timeout if timeout > 0 else None
    told: set[str] = set()
    idle_seen = 0
    while True:
        state = runner.call("GET", f"/v1/sessions/{sid}")["state"]
        for perm in state.get("permissions") or []:
            key = str(perm.get("requestId") or perm.get("id") or perm)
            if key not in told:
                told.add(key)
                tool = perm.get("toolName") or perm.get("tool") or "a tool"
                print(f"[waiting for the user to allow {tool} in the RieszManager web UI]", file=sys.stderr, flush=True)
        if state_word(state) in ("idle", "stopped"):
            idle_seen += 1
            if idle_seen >= 2:
                return state
        else:
            idle_seen = 0
        if deadline is not None and time.monotonic() > deadline:
            raise CliError(f"still working after {timeout:g}s; run `rieszctl session wait {sid[:8]}` again later")
        time.sleep(POLL_SECONDS)


def print_reply(runner: Runner, sid: str, state: dict[str, Any]) -> None:
    result = state.get("lastResult") or {}
    if result.get("isError"):
        print(f"[the turn ended with an error: {result.get('subtype') or 'error'}]", file=sys.stderr)
    if state.get("exitCode") not in (None, 0) and not state.get("alive"):
        print(f"[the claude process exited with code {state['exitCode']}]", file=sys.stderr)
    reply = last_reply(runner, sid)
    print(reply if reply else "[no reply yet]")


# ---- commands ----------------------------------------------------------------

def cmd_new(runner: Runner, args: argparse.Namespace) -> None:
    cwd = os.path.realpath(os.path.expanduser(args.directory or os.getcwd()))
    if not os.path.isdir(cwd):
        raise CliError(f"not a directory: {cwd}")
    body: dict[str, Any] = {"cwd": cwd, "text": read_message(args.message)}
    if args.model:
        body["model"] = args.model
    if args.mode:
        body["permissionMode"] = args.mode
    if args.effort:
        body["effort"] = args.effort
    if args.keep:
        body["keepAlive"] = True
    if own_session():
        body["startedBy"] = own_session()
    res = runner.call("POST", "/v1/sessions", body, timeout=180)
    sid = res["sessionId"]
    if args.json and not args.wait:
        print(json.dumps({"sessionId": sid, "cwd": cwd, "keepAlive": bool(args.keep)}, ensure_ascii=False))
        return
    print(f"Started session {sid} in {cwd}" + (" (kept running)" if args.keep else ""))
    print("It is hosted by RieszManager and shows in its web UI.", file=sys.stderr)
    if args.wait:
        print_reply(runner, sid, wait_idle(runner, sid, args.timeout))


def cmd_send(runner: Runner, args: argparse.Namespace) -> None:
    sid = resolve(runner, args.id)
    not_self(sid, "send a message to")
    res = runner.call("POST", f"/v1/sessions/{sid}/messages", {"text": read_message(args.message)}, timeout=180)
    if res.get("held"):
        print(f"Sent; {sid[:8]} is busy in a terminal, the message will be delivered once it is idle")
    else:
        print(f"Sent to {sid[:8]}")
    if args.wait:
        print_reply(runner, sid, wait_idle(runner, sid, args.timeout))


def cmd_wait(runner: Runner, args: argparse.Namespace) -> None:
    sid = resolve(runner, args.id)
    not_self(sid, "wait for")
    print_reply(runner, sid, wait_idle(runner, sid, args.timeout))


def cmd_reply(runner: Runner, args: argparse.Namespace) -> None:
    sid = resolve(runner, args.id)
    state = runner.call("GET", f"/v1/sessions/{sid}")["state"]
    if state_word(state) != "idle" and state_word(state) != "stopped":
        print(f"[{sid[:8]} is still {state_word(state)}; this is its latest message so far]", file=sys.stderr)
    print_reply(runner, sid, state)


def cmd_show(runner: Runner, args: argparse.Namespace) -> None:
    sid = resolve(runner, args.id)
    data = runner.call("GET", f"/v1/sessions/{sid}")
    summary, state = data["session"], data["state"]
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    rows = [
        ("session", sid),
        ("title", title_of(summary)),
        ("directory", summary.get("cwd") or ""),
        ("state", state_word(state) + (" (hosted)" if state.get("hosted") else " (in a terminal)" if state.get("alive") else "")),
        ("model", state.get("model") or ""),
        ("mode", state.get("permissionMode") or ""),
        ("keep running", "yes" if state.get("keepAlive") else "no"),
        ("started by", summary.get("startedBy") or ""),
        ("queued", str(len(state.get("queue") or []))),
    ]
    for perm in state.get("permissions") or []:
        rows.append(("needs approval", perm.get("toolName") or perm.get("tool") or "a tool"))
    for key, value in rows:
        if value:
            print(f"{key:>14}  {value}")


def cmd_list(runner: Runner, args: argparse.Namespace) -> None:
    me = own_session()
    sessions = all_sessions(runner)
    if args.mine:
        if not me:
            raise CliError("--mine only works inside a Claude Code session")
        sessions = [s for s in sessions if s.get("startedBy") == me]
    elif not args.all:
        sessions = [s for s in sessions if s.get("live") or s.get("keepAlive")]
    else:
        sessions = sessions[: args.limit]
    if args.json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return
    if not sessions:
        print("no sessions")
        return
    for s in sessions:
        live = s.get("live") or {}
        if live.get("waiting"):
            word = "waiting"
        elif live.get("busy"):
            word = "working"
        elif live:
            word = "idle"
        else:
            word = "stopped"
        where = "hosted" if live.get("hosted") else ("terminal" if live else "")
        flags = " ".join(f for f in (where, "kept" if s.get("keepAlive") else "", "you" if s.get("sessionId") == me else "") if f)
        title = re.sub(r"\s+", " ", title_of(s))[:60]
        print(f"{s['sessionId'][:8]}  {word:<8} {flags:<16} {s.get('cwd') or '':<36}  {title}")


def cmd_keep(runner: Runner, args: argparse.Namespace) -> None:
    sid = resolve(runner, args.id)
    on = args.state == "on"
    res = runner.call("POST", f"/v1/sessions/{sid}/keep-alive", {"on": on})
    print(f"{sid[:8]}: keep running {'on' if on else 'off'}" + (f" ({res['note']})" if res.get("note") else ""))


def cmd_stop(runner: Runner, args: argparse.Namespace) -> None:
    sid = resolve(runner, args.id)
    not_self(sid, "stop")
    res = runner.call("POST", f"/v1/sessions/{sid}/stop", {})
    if not res.get("stopped"):
        print(f"{sid[:8]} was not running here")
    else:
        print(f"Stopped {sid[:8]}" + (" (keep running is now off)" if res.get("keepAliveCleared") else ""))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rieszctl session", description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=str(Path(__file__).resolve().parents[2] / ".env"), help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def waiting(sp: argparse.ArgumentParser, flag: bool) -> None:
        if flag:
            sp.add_argument("--wait", action="store_true", help="wait until it is idle, then print its last reply")
        sp.add_argument("--timeout", type=float, default=0, metavar="SEC",
                        help="give up waiting after SEC seconds (default: no limit)")

    sp = sub.add_parser("new", help="start a hosted session (in the current directory by default)")
    sp.add_argument("-C", "--directory", metavar="DIR", help="working directory (default: the current one)")
    sp.add_argument("-m", "--model", help="model, e.g. opus or sonnet (default: Claude Code's)")
    sp.add_argument("--mode", choices=MODES, help="permission mode (default: RieszManager's)")
    sp.add_argument("--effort", choices=EFFORTS)
    sp.add_argument("--keep", action="store_true", help="keep it running instead of stopping it when idle")
    sp.add_argument("--json", action="store_true", help="print the result as JSON")
    waiting(sp, True)
    sp.add_argument("message", nargs="*", help="first message (or '-' / standard input)")
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser("send", help="send a message (starts the session again if it was stopped)")
    sp.add_argument("id")
    waiting(sp, True)
    sp.add_argument("message", nargs="*", help="the message (or '-' / standard input)")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("wait", help="wait until the session is idle, then print its last reply")
    sp.add_argument("id")
    waiting(sp, False)
    sp.set_defaults(func=cmd_wait)

    sp = sub.add_parser("reply", help="print the session's latest reply")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("show", help="show a session's status")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("list", help="list running sessions")
    sp.add_argument("--mine", action="store_true", help="sessions started by this session")
    sp.add_argument("--all", action="store_true", help="recent sessions, running or not")
    sp.add_argument("--limit", type=int, default=20, help="with --all: how many (default 20)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("keep", help="keep a session running (on) or let it stop when idle (off)")
    sp.add_argument("id")
    sp.add_argument("state", choices=["on", "off"])
    sp.set_defaults(func=cmd_keep)

    sp = sub.add_parser("stop", help="stop a session's claude process (also turns keep running off)")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_stop)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.func(Runner(Path(args.env_file)), args)
    except CliError as exc:
        print(f"rieszctl session: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
