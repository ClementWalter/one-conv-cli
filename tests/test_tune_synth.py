"""Context distillation turns real coding requests into rule-following training rows."""

import json

import pytest

from one_conv.tune.corpus import Message, Session
from one_conv.tune.dataset import write_dataset
from one_conv.tune.rules import RULES
from one_conv.tune.synth import accepted_answer, coding_prompts, synthetic_examples

RULE = RULES["no-comments"]


def _session(source="codex", key="s1"):
    return Session(source, key, "/repo", "human", 1.0, (
        Message("user", "Write a parser"), Message("assistant", "Here it is"),
        Message("user", "Now add tests"), Message("assistant", "Done")))


def test_prompts_cover_follow_ups_with_history():
    prompts = coding_prompts([_session()], valid=0.0, test=0.0)
    assert [p["messages"][-1]["content"] for p in prompts] == ["Write a parser", "Now add tests"]


def test_follow_up_carries_previous_turns():
    prompts = coding_prompts([_session()], valid=0.0, test=0.0)
    assert len(prompts[1]["messages"]) == 3


def test_chat_sources_are_skipped():
    assert coding_prompts([_session(source="chatgpt")], valid=0.0, test=0.0) == []


def test_held_out_sessions_are_skipped():
    assert coding_prompts([_session()], valid=0.0, test=1.0) == []


def test_answer_is_rewritten_to_obey_rule():
    assert accepted_answer("```py\n# setup\nx = 1\n```", RULE) == "```py\nx = 1\n```"


def test_answer_without_code_is_rejected():
    assert accepted_answer("Just restart the server.", RULE) is None


@pytest.fixture
def answers():
    return [{"key": str(i), "messages": [{"role": "user", "content": f"task {i}"}],
             "answer": "```py\n# c\nx = 1\n```"} for i in range(3)]


def test_examples_end_with_compliant_answer(answers):
    assert synthetic_examples(answers, RULE, 10)[0]["messages"][-1] == {"role": "assistant", "content": "```py\nx = 1\n```"}


def test_examples_carry_no_system_prompt(answers):
    assert [m["role"] for m in synthetic_examples(answers, RULE, 10)[0]["messages"]] == ["user", "assistant"]


def test_examples_respect_limit(answers):
    assert len(synthetic_examples(answers, RULE, 2)) == 2


def test_synthetic_rows_join_training_only(tmp_path, answers):
    write_dataset([], tmp_path, valid=0.0, test=0.0, synthetic=synthetic_examples(answers, RULE, 10))
    assert len((tmp_path / "sft" / "train.jsonl").read_text().strip().split("\n")) == 3


def test_manifest_counts_synthetic_rows(tmp_path, answers):
    manifest = write_dataset([], tmp_path, synthetic=synthetic_examples(answers, RULE, 10))
    assert manifest["synthetic_train_rows"] == 3


def test_synthetic_rows_are_valid_chat_json(tmp_path, answers):
    write_dataset([], tmp_path, synthetic=synthetic_examples(answers, RULE, 1))
    assert "messages" in json.loads((tmp_path / "sft" / "train.jsonl").read_text())
