#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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
"""Agent names and topics: binding, collisions, fan-out, catch-up positions and the envelope."""

import json
import os

import pytest
from click.testing import CliRunner

from one_conv.live import router, team
from one_conv.live.cli import build_live_group
from one_conv.live.model import LiveSession, envelope


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("omp", "OMP_HOME"),
                      ("cursor", "ONE_CONV_CURSOR_HOME"), ("live", "ONE_CONV_LIVE_DIR")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    return tmp_path


def nobody_running(address):
    return False


def _omp(homes, session_id, pid=None):
    (homes / "live" / "omp").mkdir(exist_ok=True)
    (homes / "live" / "omp" / f"{session_id}.json").write_text(
        json.dumps({"pid": pid or os.getppid(), "sessionId": session_id, "cwd": f"/work/{session_id}"}))


def _inbox(homes, session_id):
    directory = homes / "live" / "inbox" / "omp" / session_id
    return [json.loads(p.read_text())["text"] for p in sorted(directory.glob("*.json"))] if directory.is_dir() else []


# --- names --------------------------------------------------------------------------------


def test_name_resolves_to_its_address(homes):
    team.set_name("reviewer", "omp:o1", nobody_running)
    assert team.address_for("@reviewer") == "omp:o1"


def test_name_held_by_a_running_session_is_refused(homes):
    team.set_name("reviewer", "omp:o1", nobody_running)
    with pytest.raises(team.TeamError, match="running"):
        team.set_name("reviewer", "omp:o2", lambda address: True)


def test_name_held_by_a_stopped_session_needs_replace(homes):
    team.set_name("reviewer", "omp:o1", nobody_running)
    with pytest.raises(team.TeamError, match="--replace"):
        team.set_name("reviewer", "omp:o2", nobody_running)


def test_replace_takes_a_stopped_sessions_name(homes):
    team.set_name("reviewer", "omp:o1", nobody_running)
    team.set_name("reviewer", "omp:o2", nobody_running, replace=True)
    assert team.address_for("reviewer") == "omp:o2"


def test_renaming_releases_the_old_name(homes):
    team.set_name("reviewer", "omp:o1", nobody_running)
    team.set_name("builder", "omp:o1", nobody_running)
    assert team.names() == {"builder": "omp:o1"}


@pytest.mark.parametrize("bad", ["", "-lead", "a" * 33, "has space", "dot.name", "../up", "UPPER$"])
def test_invalid_name_is_refused(homes, bad):
    with pytest.raises(team.TeamError):
        team.set_name(bad, "omp:o1", nobody_running)


def test_send_to_a_name_reaches_its_session(homes):
    _omp(homes, "o1")
    team.set_name("reviewer", "omp:o1", nobody_running)
    router.send("@reviewer", "please look", LiveSession("claude", "c1"))
    assert "please look" in _inbox(homes, "o1")[0]


def test_send_to_an_unknown_name_fails_visibly(homes):
    with pytest.raises(router.LiveError, match="No agent is called"):
        router.send("@ghost", "hi", LiveSession("claude", "c1"))


def test_envelope_carries_the_senders_handle(homes):
    _omp(homes, "o1")
    team.set_name("builder", "claude:c1", nobody_running)
    router.send("omp:o1", "hi", LiveSession("claude", "c1"))
    assert 'from-handle="@builder"' in _inbox(homes, "o1")[0]


def test_reply_still_routes_by_address_not_name(homes):
    _omp(homes, "o1")
    team.set_name("builder", "claude:c1", nobody_running)
    router.send("omp:o1", "hi", LiveSession("claude", "c1"))
    assert 'to="claude:c1"' in _inbox(homes, "o1")[0]


# --- topics ---------------------------------------------------------------------------------


def test_topic_send_reaches_every_member_but_the_sender(homes):
    for session in ("o1", "o2"):
        _omp(homes, session)
        team.join("release", f"omp:{session}")
    team.join("release", "claude:c1")
    receipt = router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert sorted(row["to"] for row in receipt["deliveries"]) == ["omp:o1", "omp:o2"]


def test_topic_send_does_not_echo_to_the_sender(homes):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    router.send("#release", "tests pass", LiveSession("omp", "o1"))
    assert _inbox(homes, "o1") == []


def test_topic_envelope_names_the_topic(homes):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert 'topic="#release"' in _inbox(homes, "o1")[0]


def test_topic_envelope_offers_a_reply_to_everyone(homes):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert 'to="#release" to answer everyone' in _inbox(homes, "o1")[0]


def test_every_member_gets_the_same_message_id(homes):
    for session in ("o1", "o2"):
        _omp(homes, session)
        team.join("release", f"omp:{session}")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    ids = {text.split(' id="')[1].split('"')[0] for session in ("o1", "o2") for text in _inbox(homes, session)}
    assert len(ids) == 1


def test_unreachable_member_is_reported_not_raised(homes):
    team.join("release", "omp:gone")
    receipt = router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert receipt["deliveries"][0]["status"].startswith("not delivered")


def test_topic_send_makes_the_sender_a_member(homes):
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert team.members("release") == ["claude:c1"]


def test_direct_send_with_topic_is_recorded_on_it(homes):
    _omp(homes, "o1")
    router.send("omp:o1", "rerun the suite", LiveSession("claude", "c1"), topic="release")
    assert [row["text"] for row in team.catch_up("release", "omp:o1")["messages"]] == ["rerun the suite"]


def test_joining_catches_up_on_earlier_messages(homes):
    router.send("#release", "first", LiveSession("claude", "c1"))
    team.join("release", "omp:o1")
    assert [row["text"] for row in team.catch_up("release", "omp:o1")["messages"]] == ["first"]


def test_catch_up_shows_only_unseen_messages(homes):
    team.join("release", "omp:o1")
    router.send("#release", "first", LiveSession("claude", "c1"))
    team.catch_up("release", "omp:o1")
    router.send("#release", "second", LiveSession("claude", "c1"))
    assert [row["text"] for row in team.catch_up("release", "omp:o1")["messages"]] == ["second"]


def test_peek_keeps_messages_unseen(homes):
    team.join("release", "omp:o1")
    router.send("#release", "first", LiveSession("claude", "c1"))
    team.catch_up("release", "omp:o1", peek=True)
    assert [row["text"] for row in team.catch_up("release", "omp:o1")["messages"]] == ["first"]


def test_catch_up_excludes_other_topics(homes):
    team.join("release", "omp:o1")
    router.send("#other", "unrelated", LiveSession("claude", "c1"))
    router.send("#release", "relevant", LiveSession("claude", "c1"))
    assert [row["text"] for row in team.catch_up("release", "omp:o1")["messages"]] == ["relevant"]


def test_catch_up_past_its_budget_folds_the_oldest_block(homes):
    team.join("release", "omp:o1")
    for index in range(team.CATCH_UP + 5):
        team.record("release", f"m{index}", "claude:c1", f"message {index}")
    assert team.catch_up("release", "omp:o1")["omitted"] == 16


def test_catch_up_past_its_budget_shows_the_newest_posts_whole(homes):
    team.join("release", "omp:o1")
    for index in range(team.CATCH_UP + 5):
        team.record("release", f"m{index}", "claude:c1", f"message {index}")
    assert [m["pos"] for m in team.catch_up("release", "omp:o1")["messages"]] == list(range(16, 25))


def test_history_keeps_the_most_recent_messages(homes, monkeypatch):
    monkeypatch.setattr(team, "TOPIC_HISTORY", 3)
    for index in range(5):
        team.record("release", f"m{index}", "claude:c1", f"message {index}")
    assert [row["id"] for row in team.catch_up("release", "x", everything=True)["messages"]] == ["m2", "m3", "m4"]


def test_corrupt_history_line_does_not_hide_the_rest(homes):
    team.record("release", "m1", "claude:c1", "kept")
    with (team._topic_dir("release") / "messages.jsonl").open("a") as stream:
        stream.write("{not json\n")
    assert [row["text"] for row in team.catch_up("release", "x", everything=True)["messages"]] == ["kept"]


def test_topic_files_are_named_by_hash_not_by_topic(homes):
    team.join("release", "omp:o1")
    assert not (homes / "live" / "team" / "topics" / "release").exists()


def test_leaving_stops_delivery(homes):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    team.leave("release", "omp:o1")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert _inbox(homes, "o1") == []


def test_topics_list_members_and_counts(homes):
    team.join("release", "omp:o1")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert [(row["topic"], row["members"], row["messages"]) for row in team.topics()] == [
        ("release", ["claude:c1", "omp:o1"], 1)]


@pytest.mark.parametrize("bad", ["#", "#a b", "#../x", "topic:"])
def test_invalid_topic_target_is_refused(homes, bad):
    with pytest.raises(router.LiveError):
        router.send(bad, "hi", LiveSession("claude", "c1"))


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_join_prints_history_as_already_exchanged(homes, monkeypatch):
    router.send("#release", "first", LiveSession("claude", "c1"))
    _omp(homes, "o9", pid=os.getpid())
    monkeypatch.setattr(router, "_process", lambda pid: (1, "zsh"))
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["join", "release", "--as", "omp:o9"])
    assert "already exchanged (not new requests)" in result.output


def test_cli_topic_send_lists_each_delivery(homes, monkeypatch):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    monkeypatch.setattr(router, "_process", lambda pid: (1, "zsh"))
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["send", "#release", "go", "--yes"])
    assert "omp:o1" in result.output


def test_envelope_without_topic_has_no_topic_attribute():
    assert "topic=" not in envelope("hi", LiveSession("claude", "c1"))
