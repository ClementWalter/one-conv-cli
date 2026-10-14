"""`local feed`: per-reader positions over followed transcripts, checked on stub and real transcripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from one_conv import feed

CLI = Path(__file__).resolve().parents[1] / "bin" / "one-conv"
READER = "claude:reader-0000"
PEER = "codex:peer-0000"


@dataclass
class StubTurn:
    role: str
    text: str
    ts: str = ""
    blocks: list = field(default_factory=list)

    def __post_init__(self):
        if not self.blocks and self.text:
            self.blocks = [{"type": "text", "text": self.text}]


def tool_turn() -> StubTurn:
    return StubTurn("assistant", "", blocks=[{"type": "tool_use", "name": "Bash", "input": {}}])


def stub_thread(turns: list, address: str = PEER, stamp: str = "") -> feed.Thread:
    return feed.Thread(address=address, title="peer", cwd="/work", stamp=stamp,
                       load=lambda: turns, visible=lambda rows: rows,
                       render=lambda turn: turn.text or None)


def conversation(n: int) -> list[StubTurn]:
    return [StubTurn("user" if i % 2 == 0 else "assistant", f"turn {i}", ts=f"t{i}") for i in range(n)]


@pytest.fixture
def state(tmp_path):
    return feed.FeedState(tmp_path, READER)


def test_locate_first_read_shows_tail():
    assert feed.locate(conversation(5), None) == (None, "first")


def test_locate_after_append_resumes_at_saved_count():
    turns = conversation(4)
    saved = feed.position(turns, "")
    assert feed.locate(turns + conversation(2), saved) == (4, "ok")


def test_locate_after_prefix_rewrite_relocates_to_anchor():
    turns = conversation(4)
    saved = feed.position(turns, "")
    assert feed.locate([StubTurn("user", "inserted", ts="t-1")] + turns, saved) == (5, "relocated")


def test_locate_when_anchor_is_gone_resets():
    saved = feed.position(conversation(4), "")
    assert feed.locate([StubTurn("user", "other")] * 3, saved) == (None, "reset")


def test_locate_with_ambiguous_anchor_resets_instead_of_guessing():
    pair = [StubTurn("user", "ok"), StubTurn("assistant", "ok")]
    saved = feed.position(pair, "")
    rewritten = [StubTurn("user", "first")] + pair + [StubTurn("user", "mid")] + pair
    assert feed.locate(rewritten, saved) == (None, "reset")


def test_tail_counts_readable_turns_not_tool_calls():
    turns = conversation(4) + [tool_turn(), tool_turn(), tool_turn()]
    rows = feed.new_turns(stub_thread(turns), turns, None)
    assert [r["text"] for r in rows] == ["turn 1", "turn 2", "turn 3"]


def test_digest_then_full_still_returns_unseen_text(state):
    turns = conversation(4)
    thread = stub_thread(turns)
    feed.digest(state, {PEER: thread}, 1500, peek=False)
    assert [r["text"] for r in feed.full(state, thread, peek=False)["turns"]] == ["turn 1", "turn 2", "turn 3"]


def test_repeated_digest_without_news_prints_nothing(state):
    thread = stub_thread(conversation(4))
    feed.digest(state, {PEER: thread}, 1500, peek=False)
    assert feed.digest(state, {PEER: thread}, 1500, peek=False)["lines"] == []


def test_digest_after_append_reports_only_new_turns(state):
    turns = conversation(4)
    feed.digest(state, {PEER: stub_thread(turns)}, 1500, peek=False)
    turns += conversation(2)
    assert feed.digest(state, {PEER: stub_thread(turns)}, 1500, peek=False)["threads"][0]["new_turns"] == 2


def test_peek_leaves_notified_untouched(state):
    feed.digest(state, {PEER: stub_thread(conversation(4))}, 1500, peek=True)
    assert state.positions(PEER) == {}


def test_full_peek_leaves_read_untouched(state):
    feed.full(state, stub_thread(conversation(4)), peek=True)
    assert state.positions(PEER) == {}


def test_capped_thread_stays_eligible(state):
    other = "omp:other-0000"
    threads = {PEER: stub_thread(conversation(4)), other: stub_thread(conversation(4), other)}
    feed.digest(state, threads, 10, peek=False)
    assert "notified" not in state.positions(other)


def test_capped_thread_is_reported_as_omitted(state):
    other = "omp:other-0000"
    threads = {PEER: stub_thread(conversation(4)), other: stub_thread(conversation(4), other)}
    assert feed.digest(state, threads, 10, peek=False)["omitted"] == [other]


def test_unchanged_stamp_skips_loading(state):
    turns = conversation(4)
    feed.digest(state, {PEER: stub_thread(turns, stamp="10:1:1")}, 1500, peek=False)
    exploding = feed.Thread(PEER, "peer", "/work", "10:1:1", load=lambda: 1 / 0,
                            visible=lambda rows: rows, render=lambda turn: turn.text)
    assert feed.digest(state, {PEER: exploding}, 1500, peek=False)["lines"] == []


def test_empty_thread_then_first_turns_are_not_a_reset(state):
    turns: list = []
    feed.digest(state, {PEER: stub_thread(turns)}, 1500, peek=False)
    turns += conversation(2)
    assert feed.digest(state, {PEER: stub_thread(turns)}, 1500, peek=False)["threads"][0]["status"] == "ok"


def _unreadable_thread() -> feed.Thread:
    def load():
        raise IsADirectoryError("transcript path is a directory")
    return feed.Thread(PEER, "peer", "/work", "", load=load, visible=lambda rows: rows,
                       render=lambda turn: turn.text)


def test_unreadable_transcript_keeps_positions(state):
    feed.digest(state, {PEER: stub_thread(conversation(4))}, 1500, peek=False)
    before = json.dumps(state.positions(PEER))
    feed.digest(state, {PEER: _unreadable_thread()}, 1500, peek=False)
    assert json.dumps(state.positions(PEER)) == before


def test_unreadable_transcript_is_reported_unavailable(state):
    assert feed.digest(state, {PEER: _unreadable_thread()}, 1500, peek=False)["unavailable"] == [PEER]


def test_state_filename_never_contains_the_address(tmp_path):
    assert ":" not in feed.FeedState(tmp_path, "codex:01a1/../x").path.name


def test_state_survives_reload(tmp_path):
    with feed.locked_state(tmp_path, READER) as state:
        state.data["follows"].append(PEER)
    assert feed.FeedState(tmp_path, READER).load().data["follows"] == [PEER]


def test_state_of_another_reader_is_not_loaded(tmp_path):
    with feed.locked_state(tmp_path, READER) as state:
        state.data["follows"].append(PEER)
    assert feed.FeedState(tmp_path, "claude:someone-else").load().data["follows"] == []


@pytest.mark.parametrize("age, expected", [(3600, [PEER]), (8 * 24 * 3600, [])])
def test_peers_follow_recent_partners_only(tmp_path, age, expected):
    (tmp_path / "peers.json").write_text(json.dumps({" ".join(sorted([READER, PEER])): 1_000_000 - age}))
    assert feed.peers(tmp_path, READER, now=1_000_000) == expected


def test_peers_with_unreadable_registry_is_empty(tmp_path):
    (tmp_path / "peers.json").write_text("{")
    assert feed.peers(tmp_path, READER) == []


def test_muted_partner_is_not_followed(tmp_path):
    (tmp_path / "peers.json").write_text(json.dumps({" ".join(sorted([READER, PEER])): 9e12}))
    state = feed.FeedState(tmp_path, READER)
    state.data["muted"].append(PEER)
    assert feed.followed(state, tmp_path) == []


def _write_claude_session(home: Path, session_id: str, texts: list[str]) -> Path:
    project = home / "projects" / "-work-app"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    with path.open("a") as handle:
        for index, text in enumerate(texts):
            role = "user" if index % 2 == 0 else "assistant"
            handle.write(json.dumps({"type": role, "cwd": "/work/app", "sessionId": session_id,
                                     "timestamp": f"2026-10-14T10:00:{index:02d}Z",
                                     "message": {"role": role, "content": text}}) + "\n")
    return path


@pytest.fixture
def cli_env(tmp_path):
    env = dict(os.environ)
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("omp", "OMP_HOME"),
                      ("cursor", "CURSOR_USER_DIR"), ("cursor-cli", "CURSOR_CLI_HOME"),
                      ("state", "ONE_CONV_STATE_DIR"), ("live", "ONE_CONV_LIVE_DIR")):
        env[var] = str(tmp_path / name)
    return env


def run_feed(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), "local", "feed", "--as", READER, *args],
                          capture_output=True, text=True, env=env, check=False)


def test_cli_unfollow_cursor_agent_chat_mutes_its_live_partner(cli_env, tmp_path):
    from test_cursor_cli import CHAT_ID, _write_chat

    from one_conv.live.router import peer_key
    _write_chat(tmp_path / "cursor-cli")
    live = tmp_path / "live"
    live.mkdir()
    (live / "peers.json").write_text(json.dumps({peer_key(READER, f"cursor:{CHAT_ID}"): 9e12}))
    run_feed(cli_env, "--unfollow", f"cursor:{CHAT_ID}")
    assert json.loads(run_feed(cli_env, "--following", "--json").stdout)["following"] == []


def test_cli_follow_cursor_agent_chat_uses_the_live_address(cli_env, tmp_path):
    from test_cursor_cli import CHAT_ID, _write_chat
    _write_chat(tmp_path / "cursor-cli")
    assert run_feed(cli_env, "--follow", CHAT_ID[:8].join(["cursor:", ""])).stdout.strip() == f"following cursor:{CHAT_ID}"


def test_cli_follow_unknown_address_fails(cli_env):
    assert run_feed(cli_env, "--follow", "claude:does-not-exist").returncode == 1


def test_cli_digest_shows_followed_session(cli_env, tmp_path):
    _write_claude_session(tmp_path / "claude", "aaaa1111-0000", ["fix the parser", "parser fixed"])
    run_feed(cli_env, "--follow", "claude:aaaa1111")
    assert "parser fixed" in run_feed(cli_env).stdout


def test_cli_full_after_append_returns_only_new_turns(cli_env, tmp_path):
    home = tmp_path / "claude"
    _write_claude_session(home, "aaaa1111-0000", ["fix the parser", "parser fixed"])
    run_feed(cli_env, "--follow", "claude:aaaa1111")
    run_feed(cli_env, "--full", "claude:aaaa1111")
    _write_claude_session(home, "aaaa1111-0000", ["now add tests"])
    result = json.loads(run_feed(cli_env, "--full", "claude:aaaa1111", "--json").stdout)
    assert [row["text"] for row in result["turns"]] == ["now add tests"]
