"""Regression tests for JSON-null handling in the transcript parser.

Claude Code transcripts can contain `null` for optional objects (for example
`message.usage.server_tool_use`). `.get(key, {})` still returns None when the
key is present with a null value, so one such record used to abort the whole
Stop hook with: AttributeError: 'NoneType' object has no attribute 'get'.

Run with:  python3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tracker  # noqa: E402


def _assistant(message, **extra):
    record = {
        "type": "assistant",
        "uuid": extra.pop("uuid", "a1"),
        "sessionId": "s1",
        "timestamp": "2026-11-17T01:00:00.000Z",
        "cwd": "/tmp",
        "version": "test",
    }
    record.update(extra)
    if message is not None:
        record["message"] = message
    else:
        record["message"] = None
    return record


def _usage(**overrides):
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "server_tool_use": None,
    }
    usage.update(overrides)
    return usage


class ParseTranscriptNullTests(unittest.TestCase):
    def _parse(self, records):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "session.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        return tracker.parse_transcript(str(path))

    def test_null_server_tool_use_counts_zero_requests(self):
        parsed = self._parse([
            _assistant({"id": "m1", "model": "claude-sonnet-4-5",
                        "usage": _usage(), "content": [{"type": "text", "text": "hi"}]}),
        ])
        self.assertEqual(len(parsed["api_calls"]), 1)
        call = parsed["api_calls"][0]
        self.assertEqual(call["web_search_requests"], 0)
        self.assertEqual(call["web_fetch_requests"], 0)
        self.assertEqual(call["input_tokens"], 10)

    def test_null_message_assistant_record_is_skipped(self):
        parsed = self._parse([
            _assistant(None, uuid="a0"),
            _assistant({"id": "m1", "model": "claude-sonnet-4-5",
                        "usage": _usage(), "content": [{"type": "text", "text": "hi"}]}),
        ])
        self.assertEqual(len(parsed["api_calls"]), 1)

    def test_null_message_user_record_does_not_crash(self):
        parsed = self._parse([
            {"type": "user", "message": None, "uuid": "u0", "sessionId": "s1",
             "timestamp": "2026-11-17T01:00:00.000Z"},
            {"type": "user", "message": {"role": "user", "content": "hello"},
             "uuid": "u1", "sessionId": "s1", "timestamp": "2026-11-17T01:00:01.000Z"},
        ])
        self.assertEqual([m["content"] for m in parsed["user_messages"]], ["hello"])

    def test_null_cache_creation_object_is_tolerated(self):
        parsed = self._parse([
            _assistant({"id": "m1", "model": "claude-sonnet-4-5",
                        "usage": _usage(cache_creation=None),
                        "content": [{"type": "text", "text": "hi"}]}),
        ])
        call = parsed["api_calls"][0]
        self.assertEqual(call["cache_1h_tokens"], 0)
        self.assertEqual(call["cache_5m_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
