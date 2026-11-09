"""Incremental parsing of Claude Code session transcripts (JSONL) into display items.

A transcript file is an append-only log of heterogeneous records. Only a subset
matters for display; the rest is bookkeeping (API request blobs, file-history
snapshots, mode markers...). `TranscriptView` consumes raw lines incrementally
and maintains an ordered list of *items* that the frontend renders directly.

Item kinds:
  user          a prompt typed by a person (or sent by another session)
  assistant     one model response (one API message), holding ordered blocks:
                text / thinking / tool (a tool call with its result attached)
  command       a slash command invocation (/model, /compact ...)
  output        output printed by a local command
  bash          a `!` shell-mode command and its output
  notice        informational system lines (recaps, errors, hook messages...)
  compact       a compaction boundary, optionally carrying the summary
  interrupted   "[Request interrupted by user]"

Every item has a stable `key` and a monotonically increasing `seq`, so clients
can upsert items as the transcript grows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_RESULT_CHARS = 60_000
MAX_TEXT_CHARS = 200_000
MAX_INPUT_CHARS = 60_000

SKIP_TYPES = {
    "api-request", "api-request-blob", "api-request-shape", "file-history-snapshot",
    "file-history-delta", "mode", "session-owner", "atis-latch", "last-prompt",
    "tools-used", "cost-state", "bridge-session", "queue-operation",
}

INTERRUPT_PATTERNS = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)

_TAG_RE_CACHE: dict[str, re.Pattern[str]] = {}
_PEER_RE = re.compile(
    r'<cross-session-message[^>]*?from-name="(?P<name>[^"]*)"[^>]*>(?P<body>.*?)</cross-session-message>', re.S
)


def _tag(text: str, name: str) -> str | None:
    pattern = _TAG_RE_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(rf"<{name}>(.*?)</{name}>", re.S)
        _TAG_RE_CACHE[name] = pattern
    match = pattern.search(text)
    return match.group(1) if match else None


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _clip_json(value: Any, limit: int) -> Any:
    """Return `value` unchanged if its JSON form is small, else a truncated marker."""
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(encoded) <= limit:
        return value
    return {"__truncated__": True, "preview": encoded[:limit]}


# Structured tool results worth forwarding (keyed by tool name), with the
# fields to keep. Anything else is dropped to keep payloads small.
STRUCTURED_FIELDS: dict[str, tuple[str, ...]] = {
    "Edit": ("filePath", "structuredPatch", "replaceAll", "userModified"),
    "MultiEdit": ("filePath", "structuredPatch"),
    "Write": ("type", "filePath", "structuredPatch"),
    "NotebookEdit": ("filePath",),
    "Bash": ("stdout", "stderr", "interrupted", "returnCodeInterpretation", "backgroundTaskId", "isImage"),
    "AskUserQuestion": ("questions", "answers", "annotations"),
    "Grep": ("numFiles", "numLines", "mode"),
    "Glob": ("numFiles", "truncated"),
    "WebSearch": ("query", "durationSeconds"),
    "WebFetch": ("url", "code", "codeText", "durationMs"),
    "Read": ("type",),
}


@dataclass
class TranscriptMeta:
    """Lightweight facts gathered while parsing (used for the session list)."""

    cwd: str | None = None
    git_branch: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None
    agent_name: str | None = None
    summary: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    prompt_count: int = 0
    model: str | None = None
    permission_mode: str | None = None
    effort: str | None = None
    version: str | None = None
    entrypoint: str | None = None

    @property
    def title(self) -> str | None:
        return self.custom_title or self.ai_title or self.agent_name or self.summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "gitBranch": self.git_branch,
            "title": self.title,
            "customTitle": self.custom_title,
            "firstPrompt": self.first_prompt,
            "lastPrompt": self.last_prompt,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "promptCount": self.prompt_count,
            "model": self.model,
            "permissionMode": self.permission_mode,
            "effort": self.effort,
            "version": self.version,
            "entrypoint": self.entrypoint,
        }


def _prompt_preview(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


class MetaScanner:
    """Collects `TranscriptMeta` from records without building display items."""

    def __init__(self) -> None:
        self.meta = TranscriptMeta()

    def feed(self, rec: dict[str, Any]) -> None:
        meta = self.meta
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if isinstance(ts, str):
            if meta.created_at is None:
                meta.created_at = ts
            meta.updated_at = ts
        if rtype == "custom-title":
            meta.custom_title = rec.get("customTitle") or meta.custom_title
        elif rtype == "ai-title":
            meta.ai_title = rec.get("aiTitle") or meta.ai_title
        elif rtype == "agent-name":
            meta.agent_name = rec.get("agentName") or meta.agent_name
        elif rtype == "summary":
            meta.summary = rec.get("summary") or meta.summary
        elif rtype == "permission-mode":
            meta.permission_mode = rec.get("permissionMode") or meta.permission_mode
        if rtype in ("user", "assistant", "system", "attachment"):
            if rec.get("cwd") and meta.cwd is None:
                meta.cwd = rec["cwd"]
            if rec.get("gitBranch"):
                meta.git_branch = rec["gitBranch"]
            if rec.get("version"):
                meta.version = rec["version"]
            if rec.get("entrypoint") and meta.entrypoint is None:
                meta.entrypoint = rec["entrypoint"]
        if rtype == "assistant":
            model = (rec.get("message") or {}).get("model")
            if model and model != "<synthetic>":
                meta.model = model
                if isinstance(rec.get("effort"), str):
                    meta.effort = rec["effort"]
        elif rtype == "user" and not rec.get("isMeta") and not rec.get("isSidechain"):
            text = _plain_user_text(rec)
            origin = rec.get("origin")
            human = not isinstance(origin, dict) or origin.get("kind") in (None, "human")
            if text is not None and human and "<task-notification>" not in text:
                meta.prompt_count += 1
                preview = _prompt_preview(text)
                if meta.first_prompt is None:
                    meta.first_prompt = preview
                meta.last_prompt = preview
        elif rtype == "attachment":
            att = rec.get("attachment") or {}
            origin = att.get("origin")
            human = not isinstance(origin, dict) or origin.get("kind") in (None, "human")
            if att.get("type") == "queued_command" and isinstance(att.get("prompt"), str) and human:
                meta.prompt_count += 1
                meta.last_prompt = _prompt_preview(att["prompt"])


def _plain_user_text(rec: dict[str, Any]) -> str | None:
    """Return the human-typed text of a user record, or None if it is not a prompt."""
    if rec.get("isCompactSummary"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        if content.startswith("<") and any(
            tag in content for tag in ("<command-name>", "<local-command-", "<bash-", "<command-message>")
        ):
            return None
        if content.strip() in INTERRUPT_PATTERNS:
            return None
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
        joined = "\n".join(texts).strip()
        if joined in INTERRUPT_PATTERNS:
            return None
        if texts or any(isinstance(b, dict) and b.get("type") == "image" for b in content):
            return joined
    return None


@dataclass
class _ToolLocation:
    item_key: str
    block_index: int


@dataclass
class TranscriptView:
    """Display items built incrementally from a transcript file."""

    items: list[dict[str, Any]] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)  # key -> position in items
    # A subagent's own transcript, where every record is marked isSidechain (see subagents.py).
    sidechain: bool = False
    tools: dict[str, _ToolLocation] = field(default_factory=dict)
    meta_scanner: MetaScanner = field(default_factory=MetaScanner)
    _seq: int = 0
    _dirty: set[str] = field(default_factory=set)

    # ---- public API -------------------------------------------------------

    def feed_line(self, line: bytes | str, offset: int) -> None:
        try:
            rec = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(rec, dict):
            return
        self.meta_scanner.feed(rec)
        try:
            self._feed(rec, offset)
        except Exception:  # noqa: BLE001 - a malformed record must not break the view
            return

    def take_dirty(self) -> list[dict[str, Any]]:
        """Return items changed since the last call (in display order)."""
        if not self._dirty:
            return []
        changed = [self.items[self.index[k]] for k in self._dirty if k in self.index]
        self._dirty.clear()
        changed.sort(key=lambda it: it["seq"])
        return changed

    @property
    def meta(self) -> TranscriptMeta:
        return self.meta_scanner.meta

    # ---- item helpers -----------------------------------------------------

    def _add(self, item: dict[str, Any]) -> dict[str, Any]:
        key = item["key"]
        if key in self.index:
            # Same record seen twice (e.g. resumed sessions replaying a line): replace.
            pos = self.index[key]
            item["seq"] = self.items[pos]["seq"]
            self.items[pos] = item
        else:
            self._seq += 1
            item["seq"] = self._seq
            self.index[key] = len(self.items)
            self.items.append(item)
        self._dirty.add(key)
        return item

    def _get(self, key: str) -> dict[str, Any] | None:
        pos = self.index.get(key)
        return self.items[pos] if pos is not None else None

    def _touch(self, key: str) -> None:
        self._dirty.add(key)

    # ---- record dispatch ----------------------------------------------------

    def _feed(self, rec: dict[str, Any], offset: int) -> None:
        rtype = rec.get("type")
        if rtype in SKIP_TYPES or (rec.get("isSidechain") and not self.sidechain):
            return
        if rtype == "user":
            self._user(rec, offset)
        elif rtype == "assistant":
            self._assistant(rec, offset)
        elif rtype == "system":
            self._system(rec)
        elif rtype == "attachment":
            self._attachment(rec)

    def _base(self, rec: dict[str, Any], kind: str, key: str | None = None) -> dict[str, Any]:
        return {
            "key": key or rec.get("uuid") or f"{kind}:{self._seq + 1}",
            "kind": kind,
            "ts": rec.get("timestamp"),
            "uuid": rec.get("uuid"),
        }

    def _user(self, rec: dict[str, Any], offset: int) -> None:
        message = rec.get("message") or {}
        content = message.get("content")

        if rec.get("isCompactSummary"):
            text = content if isinstance(content, str) else _join_text(content)
            item = self._base(rec, "compact")
            item["summary"], item["truncated"] = _clip(text or "", MAX_TEXT_CHARS)
            self._add(item)
            return
        if rec.get("isMeta"):
            return

        if isinstance(content, str):
            self._user_string(rec, content)
            return
        if not isinstance(content, list):
            return

        text_parts: list[str] = []
        images: list[dict[str, Any]] = []
        for idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                self._tool_result(rec, block, idx, offset)
            elif btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "image":
                images.append(_image_ref(block, offset, ["message", "content", idx]))
            elif btype == "document":
                text_parts.append(f"[document: {(block.get('title') or 'attachment')}]")

        text = "\n".join(t for t in text_parts if t)
        if not text and not images:
            return
        if text.strip() in INTERRUPT_PATTERNS:
            item = self._base(rec, "interrupted")
            item["forTool"] = "tool use" in text
            self._add(item)
            return
        self._add_prompt(rec, text, images)

    def _user_string(self, rec: dict[str, Any], content: str) -> None:
        stripped = content.strip()
        if stripped in INTERRUPT_PATTERNS:
            item = self._base(rec, "interrupted")
            item["forTool"] = "tool use" in stripped
            self._add(item)
            return
        if "<command-name>" in content:
            item = self._base(rec, "command")
            item["name"] = (_tag(content, "command-name") or "").strip()
            item["args"] = (_tag(content, "command-args") or "").strip()
            self._add(item)
            return
        if "<local-command-stdout>" in content or "<local-command-stderr>" in content:
            out = _tag(content, "local-command-stdout")
            err = _tag(content, "local-command-stderr")
            item = self._base(rec, "output")
            item["text"], item["truncated"] = _clip((out or err or "").strip(), MAX_RESULT_CHARS)
            item["isError"] = out is None and err is not None
            self._add(item)
            return
        if "<bash-input>" in content:
            item = self._base(rec, "bash")
            item["command"] = (_tag(content, "bash-input") or "").strip()
            item["stdout"] = ""
            item["stderr"] = ""
            self._add(item)
            return
        if "<bash-stdout>" in content or "<bash-stderr>" in content:
            # Attach output to the most recent bash item.
            for it in reversed(self.items[-5:]):
                if it["kind"] == "bash":
                    it["stdout"], _ = _clip((_tag(content, "bash-stdout") or "").strip(), MAX_RESULT_CHARS)
                    it["stderr"], _ = _clip((_tag(content, "bash-stderr") or "").strip(), MAX_RESULT_CHARS)
                    self._touch(it["key"])
                    return
            return
        self._add_prompt(rec, content, [])

    def _add_prompt(self, rec: dict[str, Any], text: str, images: list[dict[str, Any]],
                    origin: Any = None, queued: bool = False) -> None:
        origin = origin if origin is not None else rec.get("origin")
        origin_kind = origin.get("kind") if isinstance(origin, dict) else None
        if "<task-notification>" in text:
            item = self._base(rec, "task")
            item["taskId"] = (_tag(text, "task-id") or "").strip()
            item["status"] = (_tag(text, "status") or "").strip()
            item["summary"] = (_tag(text, "summary") or "").strip()
            self._add(item)
            return
        item = self._base(rec, "user")
        peer = _PEER_RE.search(text)
        if peer:
            item["peerName"] = peer.group("name")
            text = peer.group("body").strip()
            origin_kind = "peer"
        item["text"], item["truncated"] = _clip(text, MAX_TEXT_CHARS)
        item["images"] = images
        item["origin"] = origin_kind
        if queued:
            item["queued"] = True
        self._add(item)

    def _tool_result(self, rec: dict[str, Any], block: dict[str, Any], idx: int, offset: int) -> None:
        tool_id = block.get("tool_use_id")
        loc = self.tools.get(tool_id or "")
        raw = block.get("content")
        text_parts: list[str] = []
        images: list[dict[str, Any]] = []
        if isinstance(raw, str):
            text_parts.append(raw)
        elif isinstance(raw, list):
            for j, sub in enumerate(raw):
                if not isinstance(sub, dict):
                    continue
                if sub.get("type") == "text":
                    text_parts.append(sub.get("text") or "")
                elif sub.get("type") == "image":
                    images.append(_image_ref(sub, offset, ["message", "content", idx, "content", j]))
        text, truncated = _clip("\n".join(text_parts), MAX_RESULT_CHARS)
        result: dict[str, Any] = {
            "text": text,
            "truncated": truncated,
            "isError": bool(block.get("is_error")),
            "images": images,
            "ts": rec.get("timestamp"),
        }
        if loc is None:
            return
        item = self._get(loc.item_key)
        if item is None:
            return
        tool_block = item["blocks"][loc.block_index]
        structured = rec.get("toolUseResult")
        fields = STRUCTURED_FIELDS.get(tool_block.get("name", ""))
        if fields and isinstance(structured, dict):
            picked = {k: structured[k] for k in fields if k in structured}
            result["structured"] = _clip_json(picked, MAX_RESULT_CHARS)
        tool_block["result"] = result
        self._touch(item["key"])

    def _assistant(self, rec: dict[str, Any], offset: int) -> None:
        message = rec.get("message") or {}
        msg_id = message.get("id") or rec.get("uuid")
        model = message.get("model")
        key = f"a:{msg_id}"
        item = self._get(key)
        if item is None:
            item = self._base(rec, "assistant", key=key)
            item["msgId"] = message.get("id")
            item["model"] = model
            item["synthetic"] = model == "<synthetic>"
            item["blocks"] = []
            item["stopReason"] = None
            self._add(item)
        if message.get("stop_reason"):
            item["stopReason"] = message.get("stop_reason")
        usage = message.get("usage")
        if isinstance(usage, dict):
            item["usage"] = {
                k: usage.get(k)
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            }
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            if not isinstance(block, dict):
                continue
            converted = self._convert_block(block, rec)
            if converted is None:
                continue
            item["blocks"].append(converted)
            if converted["type"] == "tool":
                self.tools[converted["id"]] = _ToolLocation(key, len(item["blocks"]) - 1)
        if rec.get("isApiErrorMessage") or rec.get("error"):
            item["isError"] = True
        self._touch(key)

    @staticmethod
    def _convert_block(block: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any] | None:
        btype = block.get("type")
        if btype == "text":
            text, truncated = _clip(block.get("text") or "", MAX_TEXT_CHARS)
            if not text.strip():
                return None
            return {"type": "text", "text": text, "truncated": truncated}
        if btype in ("thinking", "redacted_thinking"):
            text = block.get("thinking") or ""
            return {
                "type": "thinking",
                "text": _clip(text, MAX_TEXT_CHARS)[0],
                "redacted": btype == "redacted_thinking" or not text,
                "durationMs": rec.get("thinkingDurationMs"),
            }
        if btype in ("tool_use", "server_tool_use", "mcp_tool_use"):
            return {
                "type": "tool",
                "id": block.get("id"),
                "name": block.get("name") or "tool",
                "server": btype != "tool_use",
                "input": _clip_json(block.get("input") or {}, MAX_INPUT_CHARS),
                "result": None,
            }
        if btype and btype.endswith("_tool_result"):
            # Server-side tool results (web search etc.) arrive inside assistant content.
            return None
        return None

    def _system(self, rec: dict[str, Any]) -> None:
        subtype = rec.get("subtype")
        content = rec.get("content")
        if subtype == "compact_boundary":
            item = self._base(rec, "compact")
            meta = rec.get("compactMetadata") or {}
            item["trigger"] = meta.get("trigger")
            item["preTokens"] = meta.get("preTokens")
            item["postTokens"] = meta.get("postTokens")
            item["summary"] = None
            self._add(item)
            return
        if subtype == "local_command" and isinstance(content, str):
            out = _tag(content, "local-command-stdout")
            err = _tag(content, "local-command-stderr")
            item = self._base(rec, "output")
            text = out if out is not None else (err if err is not None else content)
            item["text"], item["truncated"] = _clip(text.strip(), MAX_RESULT_CHARS)
            item["isError"] = out is None and err is not None
            self._add(item)
            return
        if subtype in ("turn_duration", "bridge_status", "stop_hook_summary") or rec.get("isMeta"):
            return
        if isinstance(content, str) and content.strip():
            item = self._base(rec, "notice")
            item["subtype"] = subtype
            item["level"] = rec.get("level") or "info"
            item["text"], _ = _clip(content.strip(), MAX_RESULT_CHARS)
            self._add(item)

    def _attachment(self, rec: dict[str, Any]) -> None:
        att = rec.get("attachment") or {}
        atype = att.get("type")
        if atype == "queued_command":
            prompt = att.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                self._add_prompt(rec, prompt, [], origin=att.get("origin") or {}, queued=True)
        elif atype == "hook_system_message":
            text = att.get("content") or att.get("message")
            if isinstance(text, str) and text.strip():
                item = self._base(rec, "notice")
                item["subtype"] = "hook"
                item["level"] = "info"
                item["text"] = text.strip()
                self._add(item)


def _join_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else ""


def _image_ref(block: dict[str, Any], offset: int, path: list[Any]) -> dict[str, Any]:
    source = block.get("source") or {}
    return {
        "ref": f"{offset}:{'.'.join(str(p) for p in path)}",
        "mediaType": source.get("media_type") or "image/png",
    }


def resolve_image(line: bytes, path: str) -> tuple[bytes, str] | None:
    """Follow an image `ref` path inside a transcript line and return (bytes, media type)."""
    import base64

    try:
        node: Any = json.loads(line)
        for part in path.split("."):
            node = node[int(part)] if isinstance(node, list) else node[part]
        source = node.get("source") or {}
        if source.get("type") != "base64":
            return None
        return base64.b64decode(source.get("data") or ""), source.get("media_type") or "image/png"
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
