#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Write capture.json: a capture packed by the Python client, with the key and revision Python computes.

The Worker's unit tests commit it and must answer the same key and revision, so the Worker's
manifest hashing is checked against Python's own encoder, floats and non-ASCII text included.

    uv run relay-worker/test/fixtures/make_capture.py
"""

import base64
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from one_conv.archive import compress, conversation_key, encode, pack, revision  # noqa: E402

log = logging.getLogger("make-capture")


def capture(origin: str) -> dict:
    """A small Claude capture whose metadata exercises Python's float and Unicode encoding.

    The long title makes its listing row larger than the 1000 bytes from which responses are gzipped.
    """
    turns = [{"ts": "2026-01-01T10:00:00Z", "role": "user", "text": "Quartz café — naïve 🚀"},
             {"ts": "2026-01-01T10:01:00Z", "role": "assistant", "text": "Forty-two.\n\t\"quoted\""}]
    summary = {"uuid": "session-one", "source": "claude", "cwd": "/srv/quartz", "title": "Quartz 🚀 in a shared project" * 40,
               "start": "2026-01-01T10:00:00Z", "end": "2026-01-01T10:01:00Z", "turns": 2, "tokens": None,
               "ratio": 0.1, "big": 1e16, "small": 1e-05, "whole": 3.0, "count": 12345678901234567890}
    return {"origin": origin, "source": "claude", "cwd": "/srv/quartz", "session": "session-one",
            "locator": "/srv/claude/projects/-srv-quartz/session-one.jsonl", "mtime": 1767225600.0,
            "capture_version": "1", "account_label": None, "summary": summary, "unread_native": None,
            "turns": turns, "turns_with_subagents": turns,
            "search_text": [turn["text"] for turn in turns],
            "originals": {"/srv/claude/projects/-srv-quartz/session-one.jsonl": b"{}\n"}}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manifest, chunks = pack(capture("mac"))
    fixture = {
        "commit": encode({"manifest": manifest, "expected": None}).decode(),
        "chunks": {name: base64.b64encode(compress(data)).decode() for name, data in chunks.items()},
        "conversation": conversation_key("mac", "claude", "/srv/quartz", "session-one", manifest["locator"]),
        "revision": revision(manifest),
    }
    path = Path(__file__).with_name("capture.json")
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
    log.info("wrote %s", path)


if __name__ == "__main__":
    main()
