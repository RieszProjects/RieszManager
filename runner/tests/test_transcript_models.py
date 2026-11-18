"""Replies by a model other than the chosen one get `modelNote` (riesz_runner/transcript.py)."""

from __future__ import annotations

import json
import unittest

from riesz_runner.transcript import TranscriptView

FALLBACK = {"type": "system", "subtype": "model_refusal_fallback", "content": "Opus 4.8 is answering instead",
            "level": "warning", "originalModel": "claude-opus-5-5", "fallbackModel": "claude-opus-4-8"}


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def reply(mid, model, requested=None, stop="end_turn"):
    rec = {"type": "assistant", "message": {"id": mid, "role": "assistant", "model": model, "stop_reason": stop,
                                            "content": [{"type": "text", "text": "ok"}]}}
    if requested:
        rec["requestedModel"] = requested
    return rec


def notes(records):
    view = TranscriptView()
    offset = 0
    for i, rec in enumerate(records):
        line = json.dumps({"uuid": f"u{i}", "timestamp": f"2026-11-10T00:00:{i:02d}Z", **rec})
        view.feed_line(line, offset)
        offset += len(line) + 1
    return [(i["msgId"], i.get("modelNote")) for i in view.items if i["kind"] == "assistant"]


class ModelNoteTest(unittest.TestCase):
    def test_ordinary_turns_have_no_note(self):
        self.assertEqual(notes([user("a"), reply("m1", "claude-opus-5-5", "claude-opus-5-5")]), [("m1", None)])

    def test_refusal_fallback_and_the_turns_after_it(self):
        got = notes([
            user("a"),
            reply("m1", "claude-opus-5-5", "claude-opus-5-5", stop="refusal"),
            FALLBACK,
            reply("m2", "claude-opus-4-8", "claude-opus-4-8"),
            reply("m3", "claude-opus-4-8", "claude-opus-4-8"),
            user("b"),                                      # Claude Code still asks for the fallback model
            reply("m4", "claude-opus-4-8", "claude-opus-4-8"),
            user("c"),                                      # /model opus: back to the chosen model
            reply("m5", "claude-opus-5-5", "claude-opus-5-5"),
            user("d"),
            reply("m6", "claude-opus-4-8", "claude-opus-4-8"),   # chosen on purpose later: no note
        ])
        self.assertEqual(got, [("m1", None), ("m2", "claude-opus-4-8"), ("m3", None), ("m4", "claude-opus-4-8"),
                               ("m5", None), ("m6", None)])

    def test_switching_models_yourself_is_not_noted(self):
        got = notes([user("a"), reply("m1", "claude-opus-5-5", "claude-opus-5-5"),
                     user("b"), reply("m2", "claude-fable-5-2", "claude-fable-5-2")])
        self.assertEqual(got, [("m1", None), ("m2", None)])

    def test_server_side_reroute(self):
        got = notes([user("a"), reply("m1", "claude-opus-4-8", "claude-opus-5-5"), reply("m2", "claude-opus-5-5", "claude-opus-5-5")])
        self.assertEqual(got, [("m1", "claude-opus-4-8"), ("m2", None)])

    def test_old_transcripts_without_requested_model(self):
        self.assertEqual(notes([user("a"), reply("m1", "claude-opus-5-5")]), [("m1", None)])


if __name__ == "__main__":
    unittest.main()
