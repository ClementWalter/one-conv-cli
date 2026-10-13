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
