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
"""Block summaries on this machine's topics: requested on the completing post, settled once, shown by catch-up."""

import json
import os

import pytest
from click.testing import CliRunner

from one_conv.live import router, team
from one_conv.live.cli import build_live_group
from one_conv.live.model import LiveSession


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("omp", "OMP_HOME"),
                      ("cursor", "ONE_CONV_CURSOR_HOME"), ("live", "ONE_CONV_LIVE_DIR")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    monkeypatch.setattr(router, "_process", lambda pid: (1, "zsh"))
    return tmp_path


@pytest.fixture
def me(homes):
    """An omp session this process can act as (`--as omp:o9`)."""
    (homes / "live" / "omp").mkdir(exist_ok=True)
    (homes / "live" / "omp" / "o9.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "o9", "cwd": "/w"}))
    return "omp:o9"


def fill(count, start=0):
    """Record posts #start.. on #release; the request the last one returns."""
    result = None
    for index in range(start, start + count):
        result = team.record("release", f"m{index}", "claude:c1", f"message {index}")
    return result


def cli(*args):
    return CliRunner().invoke(build_live_group(["one-conv"]), list(args))


# --- requests -----------------------------------------------------------------------------


def test_record_asks_for_a_summary_when_a_block_completes(homes):
    assert fill(16)["block"] == "0-15"


def test_record_asks_nothing_before_a_block_completes(homes):
    assert fill(15) is None


def test_request_quotes_the_posts_of_the_block(homes):
    assert [p["pos"] for p in fill(16)["posts"]] == list(range(16))


def test_topic_send_receipt_carries_the_request(homes):
    receipt = None
    for index in range(16):
        receipt = router.send("#release", f"post {index}", LiveSession("claude", "c1"))
    assert receipt["summarize"]["block"] == "0-15"


def test_topic_send_receipt_has_no_request_before_a_block_completes(homes):
    assert "summarize" not in router.send("#release", "first", LiveSession("claude", "c1"))


def test_pending_request_names_the_waiting_block(homes):
    fill(20)
    assert team.pending_request("release")["block"] == "0-15"


def test_pending_request_is_none_when_nothing_waits(homes):
    fill(20)
    team.summarize("release", "0-15", "agreed", "claude:c1")
    assert team.pending_request("release") is None


# --- settling -----------------------------------------------------------------------------


def test_summarize_settles_the_block(homes):
    fill(16)
    assert team.summarize("release", "0-15", "agreed on the plan", "claude:c1")["settled"] is True


def test_summarize_keeps_the_first_line(homes):
    fill(16)
    team.summarize("release", "0-15", "first", "claude:c1")
    assert team.summarize("release", "0-15", "second", "claude:c2")["existing"] == "first"


def test_summarize_records_its_author(homes):
    fill(16)
    team.summarize("release", "0-15", "agreed", "claude:c1")
    assert json.loads((team._topic_dir("release") / "summaries.jsonl").read_text())["by"] == "claude:c1"


def test_summarize_refuses_an_incomplete_block(homes):
    fill(10)
    with pytest.raises(team.TeamError, match="not complete"):
        team.summarize("release", "0-15", "too early", "claude:c1")


def test_summarize_refuses_a_parent_before_its_halves(homes):
    fill(32)
    with pytest.raises(team.TeamError, match="halves"):
        team.summarize("release", "0-31", "too early", "claude:c1")


def test_summarize_refuses_what_is_not_a_block(homes):
    fill(32)
    with pytest.raises(team.TeamError, match="not a block"):
        team.summarize("release", "8-23", "misaligned", "claude:c1")


def test_summarize_refuses_a_long_line(homes):
    fill(16)
    with pytest.raises(team.TeamError, match="at most"):
        team.summarize("release", "0-15", "x" * 401, "claude:c1")


def test_summarize_offers_the_next_block(homes):
    fill(32)
    assert team.summarize("release", "0-15", "first", "claude:c1")["pending"]["block"] == "16-31"


def test_parent_block_is_requested_from_its_halves(homes):
    fill(32)
    team.summarize("release", "0-15", "first", "claude:c1")
    assert team.summarize("release", "16-31", "second", "claude:c1")["pending"]["halves"] == [
        {"block": "0-15", "text": "first"}, {"block": "16-31", "text": "second"}]


# --- catch-up -----------------------------------------------------------------------------


def test_catch_up_shows_the_summary_in_place_of_its_posts(homes):
    team.join("release", "omp:o1")
    fill(25)
    team.summarize("release", "0-15", "agreed on the plan", "claude:c1")
    assert team.catch_up("release", "omp:o1")["summaries"] == [{"block": "0-15", "text": "agreed on the plan"}]


def test_catch_up_marks_a_block_nobody_summarized(homes):
    team.join("release", "omp:o1")
    fill(25)
    assert team.catch_up("release", "omp:o1")["summaries"] == [{"block": "0-15", "text": None}]


def test_catch_up_asks_the_reader_for_the_missing_summary(homes):
    team.join("release", "omp:o1")
    fill(25)
    assert team.catch_up("release", "omp:o1")["summarize"]["block"] == "0-15"


def test_catch_up_asks_nothing_once_the_tree_is_complete(homes):
    team.join("release", "omp:o1")
    fill(25)
    team.summarize("release", "0-15", "agreed", "claude:c1")
    assert "summarize" not in team.catch_up("release", "omp:o1")


def test_catch_up_within_its_budget_has_no_summaries(homes):
    team.join("release", "omp:o1")
    fill(5)
    assert team.catch_up("release", "omp:o1")["summaries"] == []


def test_catch_up_within_its_budget_asks_for_nothing(homes):
    team.join("release", "omp:o1")
    fill(16)
    assert "summarize" not in team.catch_up("release", "omp:o1")


def test_returning_reader_skips_blocks_read_already(homes):
    team.join("release", "omp:o1")
    fill(20)
    team.catch_up("release", "omp:o1")
    fill(30, start=20)
    assert [b["block"] for b in team.catch_up("release", "omp:o1")["summaries"]] == ["16-31"]


def test_messages_carry_their_position(homes):
    team.join("release", "omp:o1")
    fill(3)
    assert [m["pos"] for m in team.catch_up("release", "omp:o1")["messages"]] == [0, 1, 2]


def test_positions_outlive_the_kept_history(homes, monkeypatch):
    monkeypatch.setattr(team, "TOPIC_HISTORY", 3)
    fill(5)
    assert [m["pos"] for m in team.catch_up("release", "x", everything=True)["messages"]] == [2, 3, 4]


def test_posts_recorded_before_numbering_are_numbered_in_order(homes):
    team.join("release", "omp:o1")
    team._topic_dir("release").mkdir(parents=True, exist_ok=True)
    (team._topic_dir("release") / "messages.jsonl").write_text(
        json.dumps({"id": "old", "at": 1.0, "from": "claude:c1", "text": "before numbering"}) + "\n")
    team.record("release", "new", "claude:c1", "after")
    assert [m["pos"] for m in team.catch_up("release", "omp:o1")["messages"]] == [0, 1]


# --- opening ------------------------------------------------------------------------------


def test_open_block_shows_the_posts_of_a_leaf(homes):
    fill(16)
    assert [p["pos"] for p in team.open_block("release", "0-15")["posts"]] == list(range(16))


def test_open_block_shows_the_halves_of_a_parent(homes):
    fill(32)
    team.summarize("release", "0-15", "first", "claude:c1")
    assert team.open_block("release", "0-31")["halves"] == [{"block": "0-15", "text": "first"},
                                                           {"block": "16-31", "text": None}]


def test_open_block_refuses_posts_not_exchanged_yet(homes):
    fill(5)
    with pytest.raises(team.TeamError, match="no post #16"):
        team.open_block("release", "16-31")


# --- CLI ----------------------------------------------------------------------------------


def test_cli_summarize_records_the_line(homes, me):
    fill(16)
    assert "summary recorded" in cli("summarize", "release", "0-15", "agreed on the plan", "--as", me).output


def test_cli_summarize_shows_what_waits(homes):
    fill(16)
    assert "waits for a summary of 0-15" in cli("summarize", "release").output


def test_cli_summarize_shows_the_posts_to_summarize(homes):
    fill(16)
    assert "#15 claude:c1: message 15" in cli("summarize", "release").output


def test_cli_summarize_needs_block_and_text_together(homes, me):
    assert cli("summarize", "release", "0-15", "--as", me).exit_code == 2


def test_cli_join_prints_the_summary_line(homes, me):
    fill(25)
    team.summarize("release", "0-15", "agreed on the plan", "claude:c1")
    assert "#0-15  agreed on the plan" in cli("join", "release", "--as", me).output


def test_cli_join_prints_the_request_for_a_missing_summary(homes, me):
    fill(25)
    assert "Summarize posts #0-15 of #release" in cli("join", "release", "--as", me).output


def test_cli_topic_open_prints_the_posts(homes):
    fill(16)
    assert "#3 [" in cli("topic", "release", "--open", "0-15").output


def test_cli_send_prints_the_request(homes, me):
    team.join("release", "omp:o1")
    fill(15)
    assert "Summarize posts #0-15 of #release" in cli("send", "#release", "the sixteenth", "--yes", "--from", me).output
