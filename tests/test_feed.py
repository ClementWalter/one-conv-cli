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

CLI = Path(__file__).resolve().parents[1] / "one_conv_cli.py"
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


def test_backend_error_on_one_thread_still_shows_the_others(state):
    def load():
        raise RuntimeError("archive chunk returned HTTP 404")
    broken = feed.Thread("claude:broken-0000", "broken", "/work", "", load=load,
                         visible=lambda rows: rows, render=lambda turn: turn.text)
    result = feed.digest(state, {"claude:broken-0000": broken, PEER: stub_thread(conversation(4))}, 1500, peek=False)
    assert [t["address"] for t in result["threads"]] == [PEER]


def test_unreadable_transcript_keeps_positions(state):
    feed.digest(state, {PEER: stub_thread(conversation(4))}, 1500, peek=False)
    before = json.dumps(state.positions(PEER))
    feed.digest(state, {PEER: _unreadable_thread()}, 1500, peek=False)
    assert json.dumps(state.positions(PEER)) == before


def test_unreadable_transcript_is_reported_unavailable(state):
    assert feed.digest(state, {PEER: _unreadable_thread()}, 1500, peek=False)["unavailable"] == [PEER]


def test_digest_then_append_then_full_keeps_every_unread_turn(state):
    turns = conversation(4)
    feed.digest(state, {PEER: stub_thread(turns)}, 1500, peek=False)
    turns += [StubTurn("user" if i % 2 == 0 else "assistant", f"turn {i}", ts=f"t{i}") for i in range(4, 8)]
    rows = feed.full(state, stub_thread(turns), peek=False)["turns"]
    assert [r["text"] for r in rows] == [f"turn {i}" for i in range(1, 8)]


def test_thread_dropping_to_no_turns_is_unavailable(state):
    feed.digest(state, {PEER: stub_thread(conversation(4))}, 1500, peek=False)
    assert feed.digest(state, {PEER: stub_thread([])}, 1500, peek=False)["unavailable"] == [PEER]


def test_thread_dropping_to_no_turns_keeps_positions(state):
    feed.digest(state, {PEER: stub_thread(conversation(4))}, 1500, peek=False)
    before = json.dumps(state.positions(PEER))
    feed.digest(state, {PEER: stub_thread([])}, 1500, peek=False)
    assert json.dumps(state.positions(PEER)) == before


def test_full_on_unreadable_transcript_is_unavailable(state):
    assert feed.full(state, _unreadable_thread(), peek=False)["status"] == "unavailable"


def test_first_digest_line_respects_the_cap(state):
    thread = feed.Thread(PEER, "t" * 500, "/work", "", load=lambda: conversation(4),
                         visible=lambda rows: rows, render=lambda turn: turn.text)
    assert len(feed.digest(state, {PEER: thread}, 80, peek=False)["lines"][0]) <= 80


@pytest.mark.parametrize("target, expected", [
    ("codex:01a1", "codex:01a13cb5"), ("codex:01a13cb5", "codex:01a13cb5"), ("omp:01", None)])
def test_match_address_needs_exact_or_unique_prefix(target, expected):
    assert feed.match_address(target, ["codex:01a13cb5", "codex:0bbb", "claude:01a1"]) == expected


def test_match_address_with_several_prefix_matches_is_ambiguous():
    with pytest.raises(feed.Ambiguous):
        feed.match_address("codex:0", ["codex:01a13cb5", "codex:0bbb"])


def fyi(path: Path, n: int, start: int = 0, at: float = 1000.0) -> None:
    with path.open("a") as handle:
        for i in range(start, start + n):
            handle.write(json.dumps({"id": f"m{i}", "at": at + i, "from": PEER, "to": READER,
                                     "text": f'<one-conv-message from="{PEER}" reply="none">\nnote {i}\n'
                                             f"</one-conv-message>\nNo reply expected."}) + "\n")


@pytest.fixture
def inbox(tmp_path):
    return tmp_path / "inbox.jsonl"


def test_message_body_strips_the_envelope():
    assert feed.message_body('<one-conv-message from="x">\nhello\n</one-conv-message>\nNo reply expected.') == "hello"


def test_inbox_digest_lists_each_unseen_message(state, inbox):
    fyi(inbox, 2)
    assert len(feed.inbox_digest(state, inbox, 1500, peek=False)["lines"]) == 2


def test_inbox_digest_line_shows_the_body(state, inbox):
    fyi(inbox, 1)
    assert feed.inbox_digest(state, inbox, 1500, peek=False)["lines"][0].endswith("note 0")


def test_repeated_inbox_digest_is_quiet(state, inbox):
    fyi(inbox, 2)
    feed.inbox_digest(state, inbox, 1500, peek=False)
    assert feed.inbox_digest(state, inbox, 1500, peek=False)["lines"] == []


def test_inbox_digest_then_full_still_returns_the_text(state, inbox):
    fyi(inbox, 2)
    feed.inbox_digest(state, inbox, 1500, peek=False)
    assert [m["body"] for m in feed.inbox_full(state, inbox, peek=False)] == ["note 0", "note 1"]


def test_inbox_full_after_append_returns_only_new(state, inbox):
    fyi(inbox, 2)
    feed.inbox_full(state, inbox, peek=False)
    fyi(inbox, 1, start=2)
    assert [m["body"] for m in feed.inbox_full(state, inbox, peek=False)] == ["note 2"]


def test_inbox_peek_moves_nothing(state, inbox):
    fyi(inbox, 2)
    feed.inbox_digest(state, inbox, 1500, peek=True)
    assert state.data.get("inbox") == {}


def test_inbox_cap_leaves_the_rest_pending(state, inbox):
    fyi(inbox, 3)
    feed.inbox_digest(state, inbox, 80, peek=False)
    assert len(feed.inbox_digest(state, inbox, 1500, peek=False)["lines"]) == 2


def test_inbox_after_pruning_resumes_by_time(state, inbox):
    fyi(inbox, 3)
    feed.inbox_full(state, inbox, peek=False)
    inbox.write_text("")
    fyi(inbox, 2, start=3)
    assert [m["body"] for m in feed.inbox_full(state, inbox, peek=False)] == ["note 3", "note 4"]


def test_digest_messages_carry_a_bounded_excerpt(state, inbox):
    inbox.write_text(json.dumps({"id": "big", "at": 1000.0, "from": PEER, "text": "x" * 100_000}) + "\n")
    message = feed.inbox_digest(state, inbox, 80, peek=False)["messages"][0]
    assert len(json.dumps(message)) < 400


def test_unreadable_inbox_is_reported_unavailable(state, tmp_path):
    directory = tmp_path / "inbox.jsonl"
    directory.mkdir()
    assert feed.inbox_digest(state, directory, 1500, peek=False)["unavailable"] is True


def test_unreadable_inbox_keeps_positions(state, inbox):
    fyi(inbox, 2)
    feed.inbox_full(state, inbox, peek=False)
    before = json.dumps(state.data["inbox"])
    inbox.unlink()
    inbox.mkdir()
    feed.inbox_digest(state, inbox, 1500, peek=False)
    assert json.dumps(state.data["inbox"]) == before


def test_full_on_unreadable_inbox_raises_unavailable(state, inbox):
    inbox.mkdir()
    with pytest.raises(feed.Unavailable):
        feed.inbox_full(state, inbox, peek=False)


@pytest.mark.parametrize("bad", [
    {"id": "b", "at": float("inf"), "text": "t"}, {"id": "b", "at": 1e100, "text": "t"},
    {"id": "b", "at": 10 ** 1000, "text": "t"},
    {"id": "b", "at": 1000.0, "text": None}, {"id": "b", "at": True, "text": "t"},
    {"id": 7, "at": 1000.0, "text": "t"}, {"id": "b", "at": 1000.0, "text": "t", "from": 3}])
def test_malformed_row_does_not_hide_a_later_valid_one(state, inbox, bad):
    inbox.write_text(json.dumps(bad) + "\n")
    fyi(inbox, 1, at=2000.0)
    assert [m["excerpt"] for m in feed.inbox_digest(state, inbox, 1500, peek=False)["messages"]] == ["note 0"]


def test_missing_inbox_is_empty(state, tmp_path):
    assert feed.inbox_digest(state, tmp_path / "absent.jsonl", 1500, peek=False)["lines"] == []


def test_malformed_inbox_line_is_skipped(state, inbox):
    inbox.write_text("{\n")
    fyi(inbox, 1)
    assert len(feed.inbox_digest(state, inbox, 1500, peek=False)["lines"]) == 1


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


def test_cli_follow_live_omp_session_by_prefix_before_its_transcript_exists(cli_env, tmp_path):
    registry = tmp_path / "live" / "omp"
    registry.mkdir(parents=True)
    session_id = "01a13cca-82f2-75f9-85a0-7c0f30e0c1cc"
    (registry / f"{session_id}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": session_id, "cwd": "/work/app", "title": "omp", "status": "idle"}))
    assert run_feed(cli_env, "--follow", "omp:01a13cca").stdout.strip() == f"following omp:{session_id}"


def test_cli_unfollow_after_transcript_vanished_succeeds(cli_env, tmp_path):
    path = _write_claude_session(tmp_path / "claude", "aaaa1111-0000", ["fix the parser", "parser fixed"])
    run_feed(cli_env, "--follow", "claude:aaaa1111")
    path.unlink()
    assert run_feed(cli_env, "--unfollow", "claude:aaaa1111-0000").returncode == 0


@pytest.fixture
def two_follows_one_vanished(cli_env, tmp_path):
    home = tmp_path / "claude"
    _write_claude_session(home, "aaaa1111-0000-4000-8000-000000000002", ["first", "one"])
    vanished = _write_claude_session(home, "aaaa1111-0000-4000-8000-000000000003", ["second", "two"])
    run_feed(cli_env, "--follow", "claude:aaaa1111-0000-4000-8000-000000000002")
    run_feed(cli_env, "--follow", "claude:aaaa1111-0000-4000-8000-000000000003")
    vanished.unlink()
    return cli_env


def test_cli_unfollow_ambiguous_saved_prefix_fails(two_follows_one_vanished):
    assert run_feed(two_follows_one_vanished, "--unfollow", "claude:aaaa1111", "--json").returncode == 1


def test_cli_unfollow_ambiguous_saved_prefix_keeps_both_follows(two_follows_one_vanished):
    run_feed(two_follows_one_vanished, "--unfollow", "claude:aaaa1111", "--json")
    following = json.loads(run_feed(two_follows_one_vanished, "--following", "--json").stdout)["following"]
    assert len(following) == 2


def test_cli_follow_ambiguous_local_prefix_fails(cli_env, tmp_path):
    home = tmp_path / "claude"
    _write_claude_session(home, "aaaa1111-0000-4000-8000-000000000002", ["first", "one"])
    _write_claude_session(home, "aaaa1111-0000-4000-8000-000000000003", ["second", "two"])
    assert run_feed(cli_env, "--follow", "claude:aaaa1111").returncode == 1


def test_cli_follow_json_returns_a_receipt(cli_env, tmp_path):
    _write_claude_session(tmp_path / "claude", "aaaa1111-0000", ["fix the parser", "parser fixed"])
    assert json.loads(run_feed(cli_env, "--follow", "claude:aaaa1111", "--json").stdout)["address"] == "claude:aaaa1111-0000"


def test_cli_max_chars_below_minimum_is_rejected(cli_env):
    assert run_feed(cli_env, "--max-chars", "10").returncode == 2


def test_cursor_agent_store_is_always_parsed(tmp_path, monkeypatch):
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("one_conv_launcher_feed", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    launcher = importlib.util.module_from_spec(spec)
    # Dataclasses resolve annotations through sys.modules while the module executes.
    monkeypatch.setitem(sys.modules, loader.name, launcher)
    loader.exec_module(launcher)
    store = tmp_path / "store.db"
    store.write_bytes(b"sqlite")
    handle = launcher.SessionHandle(source="cursor-cli", uuid="c1", project_cwd="/work", path=store)
    assert launcher._feed_stamp(handle) == ""


@pytest.fixture
def live_claude_target(cli_env, tmp_path):
    """A running Claude Code session record owned by a process outside the sender's ancestry."""
    process = subprocess.Popen(["sleep", "60"])
    session_id = "bbbb2222-0000-4000-8000-000000000001"
    sessions = tmp_path / "claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{process.pid}.json").write_text(json.dumps(
        {"pid": process.pid, "sessionId": session_id, "cwd": "/work/app", "name": "target",
         "status": "idle", "messagingSocketPath": str(tmp_path / "no-socket.sock")}))
    yield f"claude:{session_id}"
    process.kill()


def send_fyi(env: dict, target: str, text: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), "live", "send", target, text, "--fyi", "--yes", "--json"],
                          capture_output=True, text=True, env=env, check=False)


def test_cli_fyi_send_reports_stored_not_delivered(cli_env, live_claude_target):
    assert json.loads(send_fyi(cli_env, live_claude_target, "merged, nothing to do").stdout)["status"] == "stored FYI"


def test_cli_fyi_reaches_the_readers_feed(cli_env, live_claude_target):
    send_fyi(cli_env, live_claude_target, "merged, nothing to do")
    result = subprocess.run([sys.executable, str(CLI), "local", "feed", "--as", live_claude_target],
                            capture_output=True, text=True, env=cli_env, check=False)
    assert "merged, nothing to do" in result.stdout


def test_cli_fyi_full_inbox_returns_the_body(cli_env, live_claude_target):
    send_fyi(cli_env, live_claude_target, "merged, nothing to do")
    result = subprocess.run([sys.executable, str(CLI), "local", "feed", "--as", live_claude_target,
                             "--full", "inbox", "--json"], capture_output=True, text=True, env=cli_env, check=False)
    assert [m["body"] for m in json.loads(result.stdout)["inbox"]] == ["merged, nothing to do"]


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
