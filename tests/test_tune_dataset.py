"""Sessions become SFT targets and DPO pairs with leak-free splits."""

import json

import pytest

from one_conv.tune.corpus import Message, Session
from one_conv.tune.dataset import (
    PreferencePair,
    is_correction,
    merge_runs,
    session_examples,
    split_of,
    write_dataset,
)
from one_conv.tune.rules import RULES


def _session(*turns, key="s1"):
    return Session("claude", key, "/repo", "human", 1.0, tuple(Message(role, text) for role, text in turns))


@pytest.fixture
def corrected():
    return _session(("user", "Add caching"), ("assistant", "Added three abstraction layers"),
                    ("user", "No, keep it simple"), ("assistant", "Added one dict"),
                    ("user", "Thanks"), ("assistant", "You're welcome"))


@pytest.mark.parametrize("text", ["No, keep it simple", "don't do that", "Revert this", "pas ça", "Non, refais", "Actually use X"])
def test_detects_corrections(text):
    assert is_correction(text)


@pytest.mark.parametrize("text", ["Thanks", "Now add tests", "Notice the bug in line 3", "nothing else"])
def test_ignores_follow_ups(text):
    assert not is_correction(text)


def test_merge_joins_same_role_runs():
    messages = (Message("assistant", "a"), Message("assistant", "b"), Message("user", "c"))
    assert merge_runs(messages) == [Message("assistant", "a\n\nb"), Message("user", "c")]


def test_correction_yields_preference_pair(corrected):
    _, pairs = session_examples(corrected)
    assert pairs == [PreferencePair((Message("user", "Add caching"),), chosen="Added one dict",
                                    rejected="Added three abstraction layers", correction="No, keep it simple")]


def test_corrected_turn_is_not_an_sft_target(corrected):
    sft, _ = session_examples(corrected)
    assert [example.messages[-1].text for example in sft] == ["Added one dict", "You're welcome"]


def test_context_window_starts_on_user_turn():
    session = _session(("user", "a"), ("assistant", "b"), ("user", "c"), ("assistant", "d"))
    sft, _ = session_examples(session, max_context_messages=2)
    assert sft[-1].messages[0] == Message("user", "c")


def test_leading_assistant_turn_has_no_example():
    sft, _ = session_examples(_session(("assistant", "hello")))
    assert sft == []


def test_split_is_deterministic():
    assert split_of("claude:abc", 0.1, 0.1) == split_of("claude:abc", 0.1, 0.1)


def test_split_all_test_when_test_share_is_one():
    assert split_of("claude:abc", 0.0, 1.0) == "test"


@pytest.fixture
def written(tmp_path, corrected):
    manifest = write_dataset([corrected], tmp_path, valid=0.0, test=0.0)
    return tmp_path, manifest


def test_manifest_counts_sft(written):
    _, manifest = written
    assert manifest["counts"]["sft"]["train"] == 2


def test_manifest_counts_dpo(written):
    _, manifest = written
    assert manifest["counts"]["dpo"]["train"] == 1


def test_sft_rows_use_mlx_chat_format(written):
    root, _ = written
    first = json.loads((root / "sft" / "train.jsonl").read_text().splitlines()[0])
    assert first["messages"][-1] == {"role": "assistant", "content": "Added one dict"}


def test_rule_rewrites_targets(tmp_path):
    session = _session(("user", "Write code"), ("assistant", "```py\n# hi\nx = 1\n```"))
    write_dataset([session], tmp_path, valid=0.0, test=0.0, rule=RULES["no-comments"])
    row = json.loads((tmp_path / "sft" / "train.jsonl").read_text())
    assert row["messages"][-1]["content"] == "```py\nx = 1\n```"


def test_rule_rewrite_count_in_manifest(tmp_path):
    session = _session(("user", "Write code"), ("assistant", "```py\n# hi\nx = 1\n```"))
    manifest = write_dataset([session], tmp_path, valid=0.0, test=0.0, rule=RULES["no-comments"])
    assert manifest["rule_rewritten_targets"] == 1


def test_duplicate_sessions_are_written_once(tmp_path, corrected):
    copy = _session(*((m.role, m.text) for m in corrected.messages), key="s2")
    manifest = write_dataset([corrected, copy], tmp_path, valid=0.0, test=0.0)
    assert manifest["counts"]["dpo"]["train"] == 1


def test_duplicates_are_counted(tmp_path, corrected):
    copy = _session(*((m.role, m.text) for m in corrected.messages), key="s2")
    manifest = write_dataset([corrected, copy], tmp_path, valid=0.0, test=0.0)
    assert manifest["duplicates_skipped"] == 3


def test_budget_drops_oldest_history():
    session = _session(("user", "x" * 50), ("assistant", "y" * 50), ("user", "short"), ("assistant", "ok"))
    sft, _ = session_examples(session, max_chars=20)
    assert sft[-1].messages == (Message("user", "short"), Message("assistant", "ok"))


def test_target_longer_than_budget_is_skipped():
    sft, _ = session_examples(_session(("user", "q"), ("assistant", "z" * 100)), max_chars=50)
    assert sft == []
