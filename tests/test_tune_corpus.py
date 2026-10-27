#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
#     "mcp==2.2.0",
#     "websockets==17.2",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
#     "httpx==0.28.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Exported session documents load into training conversations."""

import json

import pytest

from one_conv.tune.corpus import Message, load_sessions, parse_session


def _doc(**overrides):
    doc = {"source": "claude", "session": "s1", "cwd": "/repo", "author": "human", "mtime": 1.0,
           "turns": [{"role": "user", "text": "Fix it"}, {"role": "assistant", "text": "Done"}]}
    doc.update(overrides)
    return doc


@pytest.fixture
def corpus(tmp_path):
    def write(name, doc):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))
    return tmp_path, write


def test_parse_keeps_user_and_assistant_text():
    assert parse_session(_doc()).messages == (Message("user", "Fix it"), Message("assistant", "Done"))


def test_parse_drops_blank_turns():
    doc = _doc(turns=[{"role": "user", "text": "  "}, {"role": "assistant", "text": "Done"}])
    assert parse_session(doc).messages == (Message("assistant", "Done"),)


@pytest.mark.parametrize("doc", [[], {"turns": "x"}, {"turns": [], "session": "s"}])
def test_parse_rejects_non_sessions(doc):
    assert parse_session(doc) is None


def test_load_keeps_newest_copy(corpus):
    root, write = corpus
    write("a/old.json", _doc(mtime=1.0, title="old"))
    write("b/new.json", _doc(mtime=2.0, turns=[{"role": "user", "text": "newer"}]))
    assert load_sessions([root])[0].messages == (Message("user", "newer"),)


def test_load_skips_system_sessions(corpus):
    root, write = corpus
    write("hb.json", _doc(author="system"))
    assert load_sessions([root]) == []


def test_load_includes_system_sessions_on_request(corpus):
    root, write = corpus
    write("hb.json", _doc(author="system"))
    assert len(load_sessions([root], include_system=True)) == 1


def test_load_skips_non_agent_sources(corpus):
    root, write = corpus
    write("wa.json", _doc(source="whatsapp"))
    assert load_sessions([root]) == []


def test_load_ignores_invalid_json(corpus):
    root, _ = corpus
    (root / "broken.json").write_text("{")
    assert load_sessions([root]) == []


@pytest.mark.parametrize("text", [
    "Tu es le même cerveau que sur tous les autres canaux. Le message arrive de telegram.",
    "Another Claude session sent a message:\n<teammate-message>",
    "Warning: apply_patch was requested via exec_command. Use the apply_patch tool",
    "This session is being continued from a previous conversation that ran out of context.",
])
def test_automated_user_turns_are_dropped(text):
    doc = _doc(turns=[{"role": "user", "text": "Fix it"}, {"role": "assistant", "text": "Done"},
                      {"role": "user", "text": text}])
    assert parse_session(doc).messages == (Message("user", "Fix it"), Message("assistant", "Done"))


def test_typed_message_is_kept():
    doc = _doc(turns=[{"role": "user", "text": "Implement the plan as specified"}])
    assert parse_session(doc).messages == (Message("user", "Implement the plan as specified"),)


def test_voice_turn_keeps_transcription():
    voice = json.dumps([{"content_type": "multimodal_text", "parts": [
        {"content_type": "audio_transcription", "text": " non, plus simple", "direction": "in"}]}])
    assert parse_session(_doc(turns=[{"role": "user", "text": voice}])).messages == (Message("user", "non, plus simple"),)


def test_plain_text_is_not_parsed_as_voice():
    assert parse_session(_doc(turns=[{"role": "user", "text": "[1, 2]"}])).messages == (Message("user", "[1, 2]"),)


def test_load_merges_same_conversation_under_two_ids(corpus):
    root, write = corpus
    write("a.json", _doc(session="old-id", mtime=1.0))
    write("b.json", _doc(session="new-id", mtime=2.0))
    assert [s.session for s in load_sessions([root])] == ["new-id"]
