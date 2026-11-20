"""Moving a conversation to another harness (handoff.py)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from riesz_runner import handoff
from riesz_runner.transcript import TranscriptView

ITEMS = [
    {"kind": "user", "key": "u1", "seq": 1, "text": "看看目录", "images": []},
    {"kind": "assistant", "key": "a1", "seq": 2, "blocks": [
        {"type": "thinking", "text": "secret reasoning", "redacted": False},
        {"type": "text", "text": "我先列一下。"},
        {"type": "tool", "id": "t1", "name": "Bash", "input": {"command": "ls"}, "result": {"text": "a.txt\nb.txt", "isError": False}},
    ]},
    {"kind": "assistant", "key": "a2", "seq": 3, "blocks": [{"type": "text", "text": "有两个文件。"}]},
    {"kind": "notice", "key": "n1", "seq": 4, "text": "overloaded — retrying"},
    {"kind": "user", "key": "u2", "seq": 5, "text": "删掉 b", "images": [{"ref": "x", "mediaType": "image/png"}]},
    {"kind": "assistant", "key": "a3", "seq": 6, "blocks": [
        {"type": "tool", "id": "t2", "name": "Bash", "input": {"command": "rm b.txt"}, "result": None}]},
    {"kind": "interrupted", "key": "i1", "seq": 7},
]


class ConversationTest(unittest.TestCase):
    def test_alternating_without_thinking(self):
        msgs = handoff.conversation(ITEMS)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        self.assertNotIn("secret reasoning", json.dumps(msgs, ensure_ascii=False))
        self.assertIn("【调用工具 Bash】ls\n【结果】a.txt\nb.txt", msgs[1]["text"])
        self.assertIn("有两个文件。", msgs[1]["text"])
        self.assertIn("[附图 1 张，没有转过来]", msgs[2]["text"])
        self.assertIn("【没有结果", msgs[3]["text"])
        self.assertIn("被用户中断", msgs[3]["text"])

    def test_long_output_is_cut(self):
        item = {"kind": "assistant", "blocks": [{"type": "tool", "name": "Read", "input": {"file_path": "/a"},
                                                 "result": {"text": "x" * 5000, "isError": False}}]}
        text = handoff.conversation([{"kind": "user", "text": "hi"}, item])[1]["text"]
        self.assertLess(len(text), 2000)
        self.assertIn("省略 3500 字", text)


class ClaudeTranscriptTest(unittest.TestCase):
    def test_written_transcript_reads_back(self):
        projects = Path(tempfile.mkdtemp())
        msgs = handoff.conversation(ITEMS)
        records = handoff.claude_records(msgs, "11111111-2222-4333-8444-555555555555", "/w/my_proj", "gpt-6.2-sol", "整理目录")
        path = handoff.write_claude_transcript(projects, "/w/my_proj", records, "11111111-2222-4333-8444-555555555555")
        self.assertEqual(path.parent.name, "-w-my-proj")
        view = TranscriptView()
        offset = 0
        for line in path.read_text().splitlines():
            view.feed_line(line, offset)
            offset += len(line) + 1
        kinds = [i["kind"] for i in view.items]
        self.assertEqual(kinds, ["user", "assistant", "user", "assistant"])
        parents = [r.get("parentUuid") for r in records if r.get("type") in ("user", "assistant")]
        uuids = [r.get("uuid") for r in records if r.get("type") in ("user", "assistant")]
        self.assertEqual(parents[1:], uuids[:-1])          # one chain
        with self.assertRaises(FileExistsError):
            handoff.write_claude_transcript(projects, "/w/my_proj", records, "11111111-2222-4333-8444-555555555555")


class CodexItemsTest(unittest.TestCase):
    def test_responses_items(self):
        items = handoff.responses_items([{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}])
        self.assertEqual(items[0], {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}]})
        self.assertEqual(items[1]["content"][0]["type"], "output_text")

    def test_display_items_come_first(self):
        shown = handoff.display_items(ITEMS, "abcdef12-0000")
        self.assertTrue(all(i["seq"] < 1 for i in shown))
        self.assertEqual([i["seq"] for i in shown], sorted(i["seq"] for i in shown))
        self.assertFalse(any(b.get("type") == "thinking" for i in shown for b in i.get("blocks") or ()))
        self.assertNotIn("notice", [i["kind"] for i in shown])


if __name__ == "__main__":
    unittest.main()
