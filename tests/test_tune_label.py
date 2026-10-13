"""Classifier labels replace the keyword heuristic when building datasets."""

import json

import pytest

from one_conv.tune.corpus import Message, Session, merge_runs
from one_conv.tune.dataset import session_examples
from one_conv.tune.label import (
    _clip,
    jev_classifier,
    label_moments,
    load_labels,
    moment_key,
    moment_state,
    moments,
)


def _session(*turns):
    return Session("claude", "s1", "/repo", "human", 1.0, tuple(Message(role, text) for role, text in turns))


@pytest.fixture
def corrected():
    return _session(("user", "Add caching"), ("assistant", "Added three abstraction layers"),
                    ("user", "I'd rather have a dict"), ("assistant", "Added one dict"),
                    ("user", "Thanks, now add tests"), ("assistant", "Tests added"))


def _labels(session, verdicts, probability=0.9):
    """Labels keyed exactly as `tune label` writes them, one verdict per answered reply."""
    rows = {}
    for moment, verdict in zip(moments(session), verdicts):
        rows[moment.key] = {"key": moment.key, "label": verdict, "probabilities": {verdict: probability}}
    return rows


def test_moments_cover_answered_replies(corrected):
    assert [moment.index for moment in moments(corrected)] == [1, 3]


def test_moment_state_names_the_request(corrected):
    assert next(moments(corrected)).state["user_request"] == "Add caching"


def test_moment_key_is_content_addressed(corrected):
    messages = merge_runs(corrected.messages)
    assert moment_key(moment_state(messages, 1)) == next(moments(corrected)).key


def test_clip_keeps_both_ends():
    assert _clip("a" * 10 + "b" * 10, 10) == "aaaaa\n[…]\nbbbbb"


def test_labeled_correction_becomes_tagged_pair(corrected):
    _, pairs = session_examples(corrected, labels=_labels(corrected, ["correction_preference", "accepted"]))
    assert [(pair.kind, pair.rejected) for pair in pairs] == [("correction_preference", "Added three abstraction layers")]


def test_correction_missed_by_keywords_is_found_by_labels(corrected):
    _, pairs = session_examples(corrected)
    assert pairs == []


def test_labeled_accept_becomes_sft(corrected):
    sft, _ = session_examples(corrected, labels=_labels(corrected, ["correction_preference", "accepted"]))
    assert [example.messages[-1].text for example in sft] == ["Added one dict"]


def test_unanswered_final_reply_is_skipped_with_labels(corrected):
    sft, _ = session_examples(corrected, labels=_labels(corrected, ["accepted", "accepted"]))
    assert "Tests added" not in [example.messages[-1].text for example in sft]


def test_low_probability_label_is_ignored(corrected):
    sft, _ = session_examples(corrected, labels=_labels(corrected, ["accepted", "accepted"], probability=0.4))
    assert sft == []


def test_unlabeled_moment_is_ignored(corrected):
    sft, pairs = session_examples(corrected, labels={})
    assert (sft, pairs) == ([], [])


@pytest.mark.parametrize("verdict", ["new_task", "question", "unclear"])
def test_neutral_labels_produce_nothing(corrected, verdict):
    sft, pairs = session_examples(corrected, labels=_labels(corrected, [verdict, verdict]))
    assert (sft, pairs) == ([], [])


class StubResponse:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = {"content-type": "application/json", **(headers or {})}

    def json(self):
        return self._payload


ANSWER = {"model": "jev-1.13.0", "usage": {"input_tokens": 300, "output_tokens": 20},
          "answers": {"reaction": {"type": "choice", "choice": "accepted",
                                   "probabilities": {"accepted": 0.9, "question": 0.1}, "confidence": 0.8}}}


def _post(responses, calls):
    def post(url, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)
    return post


def test_classifier_returns_label():
    classify = jev_classifier("k", post=_post([StubResponse(200, ANSWER)], []))
    assert classify({"x": 1})["label"] == "accepted"


def test_classifier_sends_bearer_key():
    calls = []
    jev_classifier("secret", post=_post([StubResponse(200, ANSWER)], calls))({"x": 1})
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_classifier_retries_rate_limit(monkeypatch):
    monkeypatch.setattr("one_conv.tune.label.time.sleep", lambda seconds: None)
    responses = [StubResponse(429, {}, {"retry-after": "0"}), StubResponse(200, ANSWER)]
    assert jev_classifier("k", post=_post(responses, []))({"x": 1})["model"] == "jev-1.13.0"


def test_classifier_reports_billing_error():
    billing = StubResponse(402, {"detail": {"error_type": "billing_error"}})
    with pytest.raises(RuntimeError, match="402"):
        jev_classifier("k", post=_post([billing], []))({"x": 1})


def test_label_moments_appends_rows(tmp_path, corrected):
    out = tmp_path / "labels.jsonl"
    label_moments(list(moments(corrected)), lambda state: {"label": "accepted", "probabilities": {"accepted": 1.0}}, out)
    assert len(load_labels(out)) == 2


def test_labels_resume_from_partial_file(tmp_path, corrected):
    out = tmp_path / "labels.jsonl"
    first = next(moments(corrected))
    out.write_text(json.dumps({"key": first.key, "label": "accepted", "probabilities": {}}) + "\n")
    label_moments([list(moments(corrected))[1]], lambda state: {"label": "question", "probabilities": {}}, out)
    assert sorted(row["label"] for row in load_labels(out).values()) == ["accepted", "question"]
