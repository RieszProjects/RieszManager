"""Moving a conversation to another harness: its history rewritten in the target harness's own format.

Every harness's conversation is first read into the web app's items (each driver
and the Claude Code transcript reader already produce them), then turned into a
plain conversation: the user's messages and the replies, each tool call written
as a short text (what was run, an excerpt of what came back), thinking left out.
That conversation is written the way the target harness keeps history:

- Claude Code: a transcript file under ~/.claude/projects, resumed with --resume;
- Codex: a new thread, the history added with thread/inject_items (Responses API items);
- DeepSeek Harness, Gemini CLI, Antigravity: the history goes in front of the
  first message the user sends there. (A hand-written dsh log is read once but
  breaks later: dsh needs its own system message as the first entry of a log.)

For the user it stays one session: the source is hidden and points to the new
one, which shows the source's items first (with a divider) and keeps the
rewritten history out of sight. A long conversation is cut to what follows its
last compaction; if still too long, the oldest part is left out, with the full
record in a file the new harness can read.
"""

from __future__ import annotations

import json
import re
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

TOOL_INPUT_CHARS = 400
TOOL_OUTPUT_CHARS = 1500
HARNESS_NAMES = {"claude": "Claude Code", "codex": "Codex", "dsh": "DeepSeek Harness", "gemini": "Gemini CLI",
                 "antigravity": "Antigravity"}
TARGETS = ("claude", "codex", "dsh", "gemini", "antigravity")
# How a conversation is given to a harness (HarnessSpec.handoff; Claude Code always gets a transcript):
STRATEGIES = {
    "inject-responses": "a new thread, the history added as Responses API items (Codex's thread/inject_items)",
    "first-message": "the history in front of the first message sent there (nothing to write into)",
}
PREFACE_START = "[以下是从 "
HISTORY_END = "[以上是之前的对话记录]"
BUDGET_CHARS = 240_000          # about 60–80k tokens of history; more is cut (or compacted first)
# One message to a harness that gets the history in front of the first message (UTF-8 bytes). Antigravity
# takes about 192 KB and cuts the rest, from the end, where the user's own words are: a longer history is
# sent as several messages (split_message).
MESSAGE_BYTES = 150_000
PART_NOTE = "（历史还没发完，先不要做任何事，只回复「收到」。）"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"…（省略 {len(text) - limit} 字）"


def tool_text(block: dict[str, Any]) -> str:
    """A tool call and an excerpt of its result, as text."""
    inp = block.get("input") or {}
    main = next((str(inp[k]) for k in ("command", "file_path", "path", "pattern", "url", "query", "description")
                 if isinstance(inp, dict) and inp.get(k)), None)
    if main is None:
        main = json.dumps(inp, ensure_ascii=False) if inp else ""
    out = f"【调用工具 {block.get('name')}】{_clip(main, TOOL_INPUT_CHARS)}"
    result = block.get("result")
    if result is None:
        return out + "\n【没有结果（被中断或还在运行）】"
    text = str(result.get("text") or "")
    label = "【出错】" if result.get("isError") else "【结果】"
    return out + ("\n" + label + _clip(text, TOOL_OUTPUT_CHARS) if text.strip() else "\n" + label + "（无输出）")


def conversation(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The conversation as alternating {"role", "text"} messages, user first; thinking left out."""
    msgs: list[dict[str, str]] = []

    def add(role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["text"] += "\n\n" + text
        else:
            msgs.append({"role": role, "text": text})

    for it in items:
        kind = it.get("kind")
        if kind == "user":
            text = str(it.get("text") or "")
            if it.get("images"):
                text += f"\n[附图 {len(it['images'])} 张，没有转过来]"
            add("user", text)
        elif kind == "assistant":
            parts = []
            for b in it.get("blocks") or ():
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    parts.append(b["text"])
                elif b.get("type") == "tool":
                    parts.append(tool_text(b))
            add("assistant", "\n\n".join(parts))
        elif kind == "command":
            add("user", f"{it.get('name') or ''} {it.get('args') or ''}")
        elif kind == "output":
            add("assistant", f"【命令输出】{_clip(str(it.get('text') or ''), TOOL_OUTPUT_CHARS)}")
        elif kind == "compact":
            summary = str(it.get("summary") or "")
            add("user", "[之前的对话在这里被压缩了" + (f"，摘要：\n{summary}]" if summary else "]"))
        elif kind == "interrupted":
            add("assistant", "[这一轮被用户中断了]")
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def since_last_compaction(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items from the last compaction on (its summary stands for what came before)."""
    for i in range(len(items) - 1, -1, -1):
        if items[i].get("kind") == "compact":
            return items[i:]
    return items


def size(msgs: list[dict[str, str]]) -> int:
    return sum(len(m["text"]) for m in msgs)


def fit(msgs: list[dict[str, str]], budget: int, record: str | None) -> list[dict[str, str]]:
    """The latest messages that fit the budget; a note on what was left out (and where the whole record is)."""
    if size(msgs) <= budget:
        return msgs
    kept: list[dict[str, str]] = []
    total = 0
    for m in reversed(msgs):
        if total + len(m["text"]) > budget and kept:
            break
        kept.insert(0, m)
        total += len(m["text"])
    note = f"[更早的 {len(msgs) - len(kept)} 条消息太长，没有放进来" + (f"；完整记录在 {record}" if record else "") + "]"
    if kept[0]["role"] == "user":
        kept[0] = {"role": "user", "text": note + "\n\n" + kept[0]["text"]}
    else:
        kept.insert(0, {"role": "user", "text": note})
    return kept


def record_text(msgs: list[dict[str, str]]) -> str:
    return "\n\n".join(f"【{'用户' if m['role'] == 'user' else '助手'}】{m['text']}" for m in msgs)


def history_text(msgs: list[dict[str, str]], head: str) -> str:
    """The conversation as one text, for harnesses that take it as a message (dsh, Gemini CLI, Antigravity)."""
    return head + "\n\n" + record_text(msgs) + "\n\n" + HISTORY_END


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut(text: str, limit: int) -> tuple[str, str]:
    """`text` split so the first part is at most `limit` bytes (at a character)."""
    data = text.encode("utf-8")[:limit]
    head = data.decode("utf-8", "ignore")
    return head, text[len(head):]


def split_message(history: str, text: str, limit: int = MESSAGE_BYTES) -> list[str]:
    """The history put in front of the user's message: one message, or several when it is too big for one.
    Every part starts like the history (so the web app hides it); the parts before the last ask the agent
    only to answer 「收到」; the last has the end of the history and the user's words."""
    whole = history + "\n\n" + text
    if _bytes(whole) <= limit:
        return [whole]
    head, _, rest = history.partition("\n\n")
    body = rest[: rest.rfind(HISTORY_END)].rstrip() if HISTORY_END in rest else rest
    room = limit - _bytes(head) - _bytes(PART_NOTE) - 200
    chunks: list[str] = []
    current = ""
    for record in re.split(r"\n\n(?=【(?:用户|助手)】)", body):
        while _bytes(record) > room:                  # one record bigger than a message
            space = room - _bytes(current) - 2 if current else room
            if current and space < 256:               # (almost) no room left in this part: start the next one
                chunks.append(current)
                current = ""
                continue
            piece, record = _cut(record, space)
            chunks.append(current + ("\n\n" if current else "") + piece)
            current = ""
        if current and _bytes(current) + 2 + _bytes(record) > room:
            chunks.append(current)
            current = record
        else:
            current = current + ("\n\n" if current else "") + record
    tail = "\n\n" + HISTORY_END + "\n\n" + text
    last_fits = _bytes(head) + 40 + _bytes(current) + _bytes(tail) <= limit
    if current and not last_fits:
        chunks.append(current)
        current = ""
    n = len(chunks) + 1
    parts = [f"{head}\n（第 {i}/{n} 部分）\n\n{chunk}\n\n{PART_NOTE}" for i, chunk in enumerate(chunks, 1)]
    parts.append(f"{head}\n（第 {n}/{n} 部分）" + (f"\n\n{current}" if current else "") + tail)
    return parts


def strip_history(text: str) -> str:
    """What the user wrote, without history put in front of it (empty: the message was only history)."""
    if not text.lstrip().startswith(PREFACE_START):      # a harness may hand it back with leading space
        return text
    end = text.find(HISTORY_END)
    return "" if end < 0 else text[end + len(HISTORY_END):].strip()


def preface(source_harness: str, source_model: str | None) -> str:
    name = HARNESS_NAMES.get(source_harness, source_harness)
    model = f"（模型 {source_model}）" if source_model else ""
    return (f"[以下是从 {name}{model} 会话转过来的历史记录。工具调用和结果只保留了文字摘录，思考过程没有转过来；"
            "需要细节时请重新查看文件或重新运行命令。]")


# ---- Claude Code -----------------------------------------------------------------------------

def claude_project_dir(projects_dir: Path, cwd: str) -> Path:
    """Where Claude Code keeps the transcripts of sessions started in `cwd`."""
    return projects_dir / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def claude_records(msgs: list[dict[str, str]], session_id: str, cwd: str, model: str | None,
                   title: str | None) -> list[dict[str, Any]]:
    """Transcript records for a Claude Code session holding this conversation."""
    records: list[dict[str, Any]] = []
    parent = None
    now = time.time()
    for i, m in enumerate(msgs):
        rid = str(uuidlib.uuid4())
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - len(msgs) + i)) + ".000Z"
        # rzHandoff: the web app shows the source's own items instead of these (transcript.py skips them).
        base = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": session_id,
                "uuid": rid, "timestamp": ts, "rzHandoff": True}
        if m["role"] == "user":
            records.append({**base, "type": "user", "message": {"role": "user", "content": m["text"]}})
        else:
            records.append({**base, "type": "assistant", "message": {
                "id": f"msg_handoff_{uuidlib.uuid4().hex[:20]}", "type": "message", "role": "assistant",
                "model": model or "handoff", "content": [{"type": "text", "text": m["text"]}],
                "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
        parent = rid
    if title:
        records.append({"type": "custom-title", "customTitle": title, "sessionId": session_id})
    return records


def write_claude_transcript(projects_dir: Path, cwd: str, records: list[dict[str, Any]], session_id: str) -> Path:
    folder = claude_project_dir(projects_dir, cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    with path.open("x", encoding="utf-8") as fh:                  # never overwrite a transcript
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


# ---- Codex -----------------------------------------------------------------------------------

def responses_items(msgs: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The conversation as Responses API items, for Codex's thread/inject_items."""
    out = []
    for m in msgs:
        kind = "input_text" if m["role"] == "user" else "output_text"
        out.append({"type": "message", "role": m["role"], "content": [{"type": kind, "text": m["text"]}]})
    return out


def _tag_images(images: Any, source: str) -> list[dict[str, Any]]:
    out = []
    for img in images or ():
        if not isinstance(img, dict):
            continue
        ref = str(img.get("ref") or "")
        if ref and not ref.startswith(("file:", "sid:")):
            img = {**img, "ref": f"sid:{source}:{ref}"}
        out.append(img)
    return out


def display_items(items: list[dict[str, Any]], source: str, target: str, fork: bool = False) -> list[dict[str, Any]]:
    """The source's items as shown before the new session's own, then a divider (keys and seqs of their own)."""
    kinds = ("user", "assistant", "command", "output", "compact", "interrupted", "notice")
    shown = []
    for raw in items:
        if raw.get("kind") not in kinds:
            continue
        it = dict(raw)
        if it.get("images"):
            it["images"] = _tag_images(it["images"], source)
        if it.get("kind") == "assistant" and it.get("blocks"):
            blocks = []
            for b in it["blocks"]:
                if isinstance(b, dict) and b.get("type") == "tool" and isinstance(b.get("result"), dict) and b["result"].get("images"):
                    res = {**b["result"], "images": _tag_images(b["result"]["images"], source)}
                    blocks.append({**b, "result": res})
                else:
                    blocks.append(b)
            it["blocks"] = blocks
        shown.append(it)
    shown.append({"kind": "notice", "level": "info", "subtype": "handoff", "ts": None, "uuid": None,
                  "text": "从这里分叉" if fork else f"从这里起改用 {HARNESS_NAMES.get(target, target)} 继续"})
    for n, it in enumerate(shown):
        if not str(it.get("key") or "").startswith("handoff:"):
            it["key"] = f"handoff:{source[:8]}:{it.get('key') or n}"
        it["seq"] = n - len(shown)                            # before the new session's own items (which start at 1)
        it["imported"] = True
    return shown
