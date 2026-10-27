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
"""Tone ratings rank sessions, and the local viewer carries labels next to each message."""

import json

import pytest

from one_conv.tune.corpus import Message, Session
from one_conv.tune.label import moments
from one_conv.tune.mood import intensity, mood_classifier, session_heat
from one_conv.tune.viewer import session_record, write_viewer


@pytest.mark.parametrize("probabilities, expected", [
    ({"calm": 1.0}, 0.0),
    ({"angry": 1.0}, 3.0),
    ({"calm": 0.5, "frustrated": 0.5}, 1.0),
])
def test_intensity_is_expected_level(probabilities, expected):
    assert intensity({"label": "calm", "probabilities": probabilities}) == pytest.approx(expected)


def test_intensity_falls_back_to_label():
    assert intensity({"label": "impatient", "probabilities": {}}) == 1.0


def test_session_heat_counts_frustrated_or_worse():
    assert session_heat([0.2, 1.6, 2.9]) == {"peak": 2.9, "heated": 2, "reactions": 3}


def test_empty_session_heat():
    assert session_heat([]) == {"peak": 0.0, "heated": 0, "reactions": 0}


class StubResponse:
    def __init__(self):
        self.status_code = 200
        self.headers = {}

    def json(self):
        return {"results": [{"label": "angry", "confidence": 0.9, "scores": {"angry": 0.9, "calm or neutral": 0.1}}]}


def test_mood_classifier_maps_worded_labels():
    rows = mood_classifier(post=lambda url, **kw: StubResponse())([{"assistant_reply": "a", "user_next_message": "b"}])
    assert rows[0]["probabilities"] == {"angry": 0.9, "calm": 0.1}


def test_mood_classifier_shares_no_data():
    calls = []
    mood_classifier(post=lambda url, **kw: calls.append(kw) or StubResponse())([{"assistant_reply": "a", "user_next_message": "b"}])
    assert calls[0]["json"]["share_data"] is False


@pytest.fixture
def session():
    return Session("codex", "s1", "/repo", "human", 1.0, (
        Message("user", "Add caching"), Message("assistant", "Added three layers"),
        Message("user", "NO. Use a dict."), Message("assistant", "Done")))


@pytest.fixture
def labeled(session):
    key = next(moments(session)).key
    return ({key: {"label": "correction_preference", "probabilities": {"correction_preference": 0.93}}},
            {key: {"label": "angry", "probabilities": {"angry": 1.0}}})


def test_record_marks_reaction_on_user_answer(session, labeled):
    assert session_record(session, *labeled)["messages"][2]["reaction"] == ["correction_preference", 0.93]


def test_record_marks_tone_on_user_message(session, labeled):
    assert session_record(session, *labeled)["messages"][2]["mood"] == 3.0


def test_record_counts_heated_reactions(session, labeled):
    assert session_record(session, *labeled)["heated"] == 1


def test_viewer_embeds_sessions(tmp_path, session, labeled):
    out = tmp_path / "viewer.html"
    write_viewer([session], *labeled, out)
    data = out.read_text().split('type="application/json">')[1].split("</script>")[0]
    assert json.loads(data)[0]["id"] == "codex:s1"


def test_viewer_escapes_script_close(tmp_path, labeled):
    tricky = Session("codex", "s2", "/repo", "human", 1.0, (Message("user", "</script><b>x</b>"),))
    out = tmp_path / "viewer.html"
    write_viewer([tricky], *labeled, out)
    assert out.read_text().count("</script>") == 2


def test_identical_sessions_appear_once(session):
    from one_conv.tune.viewer import unique_sessions
    copy = Session("codex", "rollout-s1", "/repo", "human", 2.0, session.messages)
    assert [s.session for s in unique_sessions([session, copy])] == ["rollout-s1"]
