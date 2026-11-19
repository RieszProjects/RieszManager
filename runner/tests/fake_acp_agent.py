#!/usr/bin/env python3
"""A scripted ACP agent for testing drivers/acp.py (run as `fake_acp_agent.py --acp --skip-trust`).

session/prompt text decides what happens: "edit" asks permission for an edit and reports the diff;
"wait" streams a little, then waits for session/cancel; anything else is answered with "echo: <text>".
"""

import json
import sys
import threading

lock = threading.Lock()
pending = {}
next_id = [5000]
cancelled = threading.Event()
mode = ["default"]


def send(msg):
    with lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        sys.stdout.flush()


def update(sid, u):
    send({"method": "session/update", "params": {"sessionId": sid, "update": u}})


def ask(params):
    next_id[0] += 1
    ev = threading.Event()
    pending[next_id[0]] = [ev, None]
    send({"id": next_id[0], "method": "session/request_permission", "params": params})
    ev.wait()
    return pending.pop(next_id[0])[1]


def prompt(rid, p):
    sid = p["sessionId"]
    text = "".join(b.get("text", "") for b in p["prompt"] if b.get("type") == "text")
    images = sum(1 for b in p["prompt"] if b.get("type") == "image")
    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking…"}})
    if text == "edit":
        call = {"toolCallId": "t1", "title": "WriteFile notes.txt", "kind": "edit", "status": "pending",
                "content": [{"type": "diff", "path": "/w/notes.txt", "oldText": "a\n", "newText": "b\n"}],
                "locations": [{"path": "/w/notes.txt"}]}
        update(sid, {"sessionUpdate": "tool_call", **call})
        answer = ask({"sessionId": sid, "toolCall": call, "options": [
            {"optionId": "proceed_always", "name": "Allow for this session", "kind": "allow_always"},
            {"optionId": "proceed_once", "name": "Allow", "kind": "allow_once"},
            {"optionId": "cancel", "name": "Reject", "kind": "reject_once"}]})
        outcome = answer["outcome"]
        chosen = outcome.get("optionId", outcome["outcome"])
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                     "status": "completed" if chosen.startswith("proceed") else "failed",
                     "content": [{"type": "content", "content": {"type": "text", "text": f"chose {chosen}"}}]})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"done {chosen}"}})
    elif text == "wait":
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t2", "title": "sleep 60", "kind": "execute", "status": "in_progress"})
        cancelled.wait(30)
        cancelled.clear()
        send({"id": rid, "result": {"stopReason": "cancelled"}})
        return
    else:
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "echo: "}})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"{text} [{images} images, {mode[0]}]"}})
    update(sid, {"sessionUpdate": "usage_update", "used": 1200, "size": 1000000})
    send({"id": rid, "result": {"stopReason": "end_turn"}})


SESSION = {"modes": {"currentModeId": "default", "availableModes": [{"id": "default", "name": "Default"}, {"id": "yolo", "name": "YOLO"}]},
           "models": {"currentModelId": "gemini-pro", "availableModels": [{"modelId": "gemini-pro", "name": "Gemini Pro"},
                                                                          {"modelId": "gemini-flash", "name": "Gemini Flash"}]}}

for line in sys.stdin:
    msg = json.loads(line)
    if "method" not in msg:
        slot = pending.get(msg["id"])
        if slot:
            slot[1] = msg.get("result")
            slot[0].set()
        continue
    method, rid, p = msg["method"], msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "authMethods": []}})
    elif method == "session/new":
        send({"id": rid, "result": {"sessionId": "11111111-2222-4333-8444-555555555555", **SESSION}})
    elif method == "session/load":
        sid = p["sessionId"]
        update(sid, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "earlier question"}})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "earlier answer"}})
        send({"id": rid, "result": SESSION})
    elif method == "session/prompt":
        threading.Thread(target=prompt, args=(rid, p), daemon=True).start()
    elif method == "session/cancel":
        cancelled.set()
    elif method == "session/set_mode":
        mode[0] = p["modeId"]
        update(p["sessionId"], {"sessionUpdate": "current_mode_update", "currentModeId": p["modeId"]})
        send({"id": rid, "result": {}})
    elif method == "session/set_model":
        send({"id": rid, "result": {}})
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": f"no {method}"}})
