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
"""Classifier labels replace the keyword heuristic when building datasets."""

import json

import pytest

from one_conv.tune.corpus import Message, Session, merge_runs
from one_conv.tune.dataset import session_examples
from one_conv.tune.label import (
    PROVIDERS,
    _clip,
    classifier_dev_classifier,
    decision_classifier,
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


@pytest.mark.parametrize("verdict", ["question", "unclear"])
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
    classify = decision_classifier(PROVIDERS["jev"], "k", post=_post([StubResponse(200, ANSWER)], []))
    assert classify([{"x": 1}])[0]["label"] == "accepted"


def test_classifier_sends_bearer_key():
    calls = []
    decision_classifier(PROVIDERS["jev"], "secret", post=_post([StubResponse(200, ANSWER)], calls))([{"x": 1}])
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_classifier_retries_rate_limit(monkeypatch):
    monkeypatch.setattr("one_conv.tune.label.time.sleep", lambda seconds: None)
    responses = [StubResponse(429, {}, {"retry-after": "0"}), StubResponse(200, ANSWER)]
    assert decision_classifier(PROVIDERS["jev"], "k", post=_post(responses, []))([{"x": 1}])[0]["model"] == "jev-1.13.0"


def test_classifier_reports_billing_error():
    billing = StubResponse(402, {"detail": {"error_type": "billing_error"}})
    with pytest.raises(RuntimeError, match="402"):
        decision_classifier(PROVIDERS["jev"], "k", post=_post([billing], []))([{"x": 1}])


def test_label_moments_appends_rows(tmp_path, corrected):
    out = tmp_path / "labels.jsonl"
    label_moments(list(moments(corrected)), lambda states: [{"label": "accepted", "probabilities": {"accepted": 1.0}} for _ in states], out)
    assert len(load_labels(out)) == 2


def test_labels_resume_from_partial_file(tmp_path, corrected):
    out = tmp_path / "labels.jsonl"
    first = next(moments(corrected))
    out.write_text(json.dumps({"key": first.key, "label": "accepted", "probabilities": {}}) + "\n")
    label_moments([list(moments(corrected))[1]], lambda states: [{"label": "question", "probabilities": {}} for _ in states], out)
    assert sorted(row["label"] for row in load_labels(out).values()) == ["accepted", "question"]


def test_subq_uses_its_own_endpoint():
    calls = []
    post = _post([StubResponse(200, ANSWER)], calls)
    urls = []
    decision_classifier(PROVIDERS["subq"], "k", post=lambda url, **kw: urls.append(url) or post(url, **kw))([{"x": 1}])
    assert urls == ["https://api.subq.ai/v1/systemone"]


def test_subq_sends_snap_model():
    calls = []
    decision_classifier(PROVIDERS["subq"], "k", post=_post([StubResponse(200, ANSWER)], calls))([{"x": 1}])
    assert calls[0]["json"]["model"] == "subq-snap-preview"


CDEV = {"results": [{"label": "correction the assistant could have anticipated", "confidence": 0.98,
                     "scores": {"correction the assistant could have anticipated": 0.98, "accepted the reply": 0.02},
                     "model": "jev-1.13.0"}]}
STATE = {"user_request": "a", "assistant_reply": "b", "user_next_message": "c"}


def test_classifier_dev_maps_worded_label_back():
    classify = classifier_dev_classifier(post=_post([StubResponse(200, CDEV)], []))
    assert classify([STATE])[0]["label"] == "correction_preference"


def test_classifier_dev_maps_scores_back():
    classify = classifier_dev_classifier(post=_post([StubResponse(200, CDEV)], []))
    assert classify([STATE])[0]["probabilities"] == {"correction_preference": 0.98, "accepted": 0.02}


def test_classifier_dev_opts_out_of_training_in_body():
    calls = []
    classifier_dev_classifier(post=_post([StubResponse(200, CDEV)], calls))([STATE])
    assert calls[0]["json"]["share_data"] is False


def test_classifier_dev_opts_out_of_training_in_header():
    calls = []
    classifier_dev_classifier(post=_post([StubResponse(200, CDEV)], calls))([STATE])
    assert calls[0]["headers"]["X-Classifier-Share-Data"] == "false"


def test_classifier_dev_sends_whole_batch_in_one_call():
    calls = []
    two = {"results": CDEV["results"] * 2}
    classifier_dev_classifier(post=_post([StubResponse(200, two)], calls))([STATE, STATE])
    assert len(calls[0]["json"]["inputs"]) == 2


def test_batched_labeling_writes_every_moment(tmp_path, corrected):
    out = tmp_path / "labels.jsonl"
    label_moments(list(moments(corrected)), lambda states: [{"label": "accepted", "probabilities": {}} for _ in states],
                  out, batch_size=40)
    assert len(load_labels(out)) == 2


def test_new_task_counts_as_acceptance(corrected):
    sft, _ = session_examples(corrected, labels=_labels(corrected, ["new_task", "new_task"]))
    assert [example.messages[-1].text for example in sft] == ["Added three abstraction layers", "Added one dict"]


def test_new_info_pairs_stay_out_of_dpo(tmp_path, corrected):
    from one_conv.tune.dataset import write_dataset
    manifest = write_dataset([corrected], tmp_path, valid=0.0, test=0.0,
                             labels=_labels(corrected, ["correction_new_info", "accepted"]))
    assert (manifest["counts"]["dpo"]["train"], manifest["counts"]["new_info"]["train"]) == (0, 1)


def _chat_response(content, status=200):
    return StubResponse(status, {"model": "gpt-6-luna", "usage": {"prompt_tokens": 300},
                                 "choices": [{"message": {"content": content}}]})


def test_llm_judge_reads_json_label():
    from one_conv.tune.label import llm_classifier
    classify = llm_classifier("k", "gpt-6-luna", post=_post([_chat_response('{"label": "question", "confidence": 0.8}')], []))
    assert classify([STATE])[0]["probabilities"] == {"question": 0.8}


def test_llm_judge_unknown_label_becomes_unclear():
    from one_conv.tune.label import llm_classifier
    classify = llm_classifier("k", "m", post=_post([_chat_response('{"label": "praise", "confidence": 0.9}')], []))
    assert classify([STATE])[0]["label"] == "unclear"


def test_llm_judge_unparseable_answer_becomes_unclear():
    from one_conv.tune.label import llm_classifier
    classify = llm_classifier("k", "m", post=_post([_chat_response("I think it's a correction")], []))
    assert classify([STATE])[0]["label"] == "unclear"


def test_llm_judge_sends_chosen_model():
    from one_conv.tune.label import llm_classifier
    calls = []
    llm_classifier("k", "claude-opus-5.5", post=_post([_chat_response('{"label": "accepted", "confidence": 1}')], calls))([STATE])
    assert calls[0]["json"]["model"] == "claude-opus-5.5"


def test_judge_overrides_only_uncertain_labels():
    from one_conv.tune.label import adjudicated
    primary = {"a": {"label": "accepted", "probabilities": {"accepted": 0.9}},
               "b": {"label": "question", "probabilities": {"question": 0.4}}}
    judge = {"a": {"label": "unclear", "probabilities": {"unclear": 1.0}},
             "b": {"label": "correction_preference", "probabilities": {"correction_preference": 0.9}}}
    assert {k: v["label"] for k, v in adjudicated(primary, judge, 0.6).items()} == {"a": "accepted", "b": "correction_preference"}


def test_labels_file_name_includes_judge_model():
    from one_conv.tune.label import labels_name
    assert labels_name("boundless", "gpt-6-luna") == "boundless-gpt-6-luna"
