"""Isolated Cursor CLI backend coverage for one-conv.

Each test runs the real CLI against a temp $CURSOR_CLI_HOME so it never
touches the machine's live ~/.cursor / ~/.claude / ~/.codex history.
"""

from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "bin" / "one-conv"

CHAT_ID = "59a98c57-0000-4000-8000-000000000001"
CWD = "/tmp/one-conv-cursor-cli-fixture"
PROMPT = "<timestamp>Tuesday, Sep 15, 2026, 2:49 PM (UTC+2)</timestamp>\n<user_query>\nplease fix the widget\n</user_query>"


def _write_store(chat_dir: Path, messages: list) -> None:
    """A checkpointed WAL store with no `-shm` sidecar, which `mode=ro` cannot open."""
    chat_dir.mkdir(parents=True)
    conn = sqlite3.connect(chat_dir / "store.db")
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for i, message in enumerate(messages):
        data = message if isinstance(message, bytes) else json.dumps(message).encode()
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (f"{i:064x}", data))
    conn.commit()
    conn.close()
    for sidecar in ("store.db-wal", "store.db-shm"):
        (chat_dir / sidecar).unlink(missing_ok=True)


def _write_chat(cursor_home: Path) -> None:
    workspace = cursor_home / "chats" / "e0a7de0fcb37071e9b4f94368cae7ac1"
    chat_dir = workspace / CHAT_ID
    _write_store(
        chat_dir,
        [
            {"role": "system", "content": "You are an AI coding assistant, powered by Cursor."},
            {"role": "user", "content": "<user_info>\nOS Version: darwin\n</user_info>"},
            b"\n\x87\x05binary protobuf row",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<manually_attached_skills>\nfix-widget\n</manually_attached_skills>"},
                    {"type": "text", "text": PROMPT},
                ],
            },
            {
                "role": "assistant",
                "id": "msg_1",
                "content": [
                    {"type": "reasoning", "text": "I will edit the file."},
                    {"type": "tool-call", "toolCallId": "call-1", "toolName": "edit", "args": {"path": "widget.ts"}},
                ],
            },
            {
                "role": "tool",
                "id": "call-1",
                "content": [{"type": "tool-result", "toolCallId": "call-1", "toolName": "edit", "result": "ok"}],
            },
            {"role": "assistant", "id": "msg_2", "content": [{"type": "text", "text": "Widget patched."}]},
        ],
    )
    (chat_dir / "meta.json").write_text(json.dumps({
        "schemaVersion": 1,
        "createdAtMs": 1789483816808,
        "updatedAtMs": 1789571433281,
        "hasConversation": True,
        "title": "Fix the widget",
        "cwd": CWD,
    }))
    # A subagent run: store only, no meta.json.
    _write_store(workspace / "0c5cf32e-0000-4000-8000-000000000002", [{"role": "user", "content": "subagent"}])
    # A chat that was opened and never prompted.
    empty = workspace / "4663a5be-0000-4000-8000-000000000003"
    empty.mkdir()
    (empty / "meta.json").write_text(json.dumps({"schemaVersion": 1, "hasConversation": False, "cwd": CWD}))
    # A truncated meta.json that is valid JSON but not an object.
    broken = workspace / "86cd7f43-0000-4000-8000-000000000004"
    broken.mkdir()
    (broken / "meta.json").write_text("null")


@pytest.fixture()
def cursor_cli_env(tmp_path: Path) -> dict[str, str]:
    cursor_home = tmp_path / "cursor-cli"
    _write_chat(cursor_home)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = os.environ.copy()
    env["CURSOR_CLI_HOME"] = str(cursor_home)
    env["CLAUDE_CONFIG_DIR"] = str(empty / "claude")
    env["CODEX_HOME"] = str(empty / "codex")
    env["CURSOR_USER_DIR"] = str(empty / "cursor")
    env["OMP_HOME"] = str(empty / "omp")
    env["AGENT_CONV_STATE_DIR"] = str(tmp_path / "state")
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(CLI), *args], check=True, capture_output=True, text=True, env=env)


def _thread(env: dict[str, str], *extra: str) -> dict:
    return json.loads(_run(env, "thread", "one-conv-cursor-cli-fixture", "--source", "cursor-cli", "--json", *extra).stdout)


def test_chats_lists_the_project_with_one_thread(cursor_cli_env: dict[str, str]) -> None:
    projects = json.loads(_run(cursor_cli_env, "chats", "--source", "cursor-cli", "--json").stdout)
    assert [(p["source"], p["cwd"], p["threads"]) for p in projects] == [("cursor-cli", CWD, 1)]


def test_read_uses_the_stored_title_and_chat_id(cursor_cli_env: dict[str, str]) -> None:
    threads = json.loads(_run(cursor_cli_env, "read", "one-conv-cursor-cli-fixture", "--source", "cursor-cli", "--json").stdout)
    assert [(t["title"], t["uuid"]) for t in threads] == [("Fix the widget", CHAT_ID)]


def test_thread_compact_shows_only_the_typed_prompt_and_prose(cursor_cli_env: dict[str, str]) -> None:
    payload = _thread(cursor_cli_env, "--no-mark-read")
    assert [(t["role"], t["text"]) for t in payload["turns"]] == [
        ("user", "please fix the widget"),
        ("assistant", "Widget patched."),
    ]


def test_prompt_timestamp_is_parsed_with_its_offset(cursor_cli_env: dict[str, str]) -> None:
    payload = _thread(cursor_cli_env, "--no-mark-read")
    assert payload["turns"][0]["ts"] == "2026-09-15T14:49:00+02:00"


def test_assistant_turn_carries_the_preceding_prompt_timestamp(cursor_cli_env: dict[str, str]) -> None:
    payload = _thread(cursor_cli_env, "--no-mark-read")
    assert payload["turns"][1]["ts"] == "2026-09-15T14:49:00+02:00"


def test_thread_raw_includes_thinking_tool_call_and_result(cursor_cli_env: dict[str, str]) -> None:
    joined = "\n".join(t["text"] for t in _thread(cursor_cli_env, "--raw", "--no-mark-read")["turns"])
    assert "[thinking] I will edit the file." in joined
    assert "→ edit(" in joined
    assert "← ok" in joined


def test_environment_preamble_is_not_a_turn(cursor_cli_env: dict[str, str]) -> None:
    joined = "\n".join(t["text"] for t in _thread(cursor_cli_env, "--raw", "--no-mark-read")["turns"])
    assert "user_info" not in joined
    assert "manually_attached_skills" not in joined


def test_search_finds_the_typed_prompt(cursor_cli_env: dict[str, str]) -> None:
    hits = json.loads(_run(cursor_cli_env, "search", "fix the widget", "--source", "cursor-cli", "--json").stdout)
    assert [(h["session"], h["role"]) for h in hits] == [(CHAT_ID, "user")]


def test_find_matches_the_stored_title(cursor_cli_env: dict[str, str]) -> None:
    hits = json.loads(_run(cursor_cli_env, "find", "widget", "--source", "cursor-cli", "--json").stdout)
    assert [h["uuid"] for h in hits] == [CHAT_ID]


def test_reading_a_thread_clears_its_local_unread_mark(cursor_cli_env: dict[str, str]) -> None:
    before = json.loads(_run(cursor_cli_env, "unread", "--source", "cursor-cli", "--json").stdout)
    _thread(cursor_cli_env)
    after = json.loads(_run(cursor_cli_env, "unread", "--source", "cursor-cli", "--json").stdout)
    assert ([h["uuid"] for h in before], after) == ([CHAT_ID], [])


@pytest.fixture()
def backend():
    return runpy.run_path(str(CLI), run_name="cursor_cli_review")


@pytest.mark.parametrize("name", ["cursor#home", "cursor?home", "cursor%23home", "cursor home"])
def test_store_path_preserves_uri_characters(backend, tmp_path, name):
    chat = tmp_path / name
    _write_store(chat, [b'{}'])
    assert backend["_cursor_cli_rows"](chat / "store.db") == [b'{}']


def test_store_read_does_not_create_a_truncated_path(backend, tmp_path):
    chat = tmp_path / "cursor#home"
    _write_store(chat, [b'{}'])
    backend["_cursor_cli_rows"](chat / "store.db")
    assert not (tmp_path / "cursor").exists()


def test_missing_store_is_not_created(backend, tmp_path):
    db = tmp_path / "store.db"
    backend["_cursor_cli_rows"](db)
    assert not db.exists()


def test_live_store_reads_committed_wal_rows(backend, tmp_path):
    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=wal")
        conn.execute("CREATE TABLE blobs (data BLOB)")
        conn.execute("INSERT INTO blobs VALUES (?)", (b'{}',))
        conn.commit()
        assert backend["_cursor_cli_rows"](db) == [b'{}']
    finally:
        conn.close()


@pytest.mark.parametrize("stamp", [
    "Tuesday, Sep 99, 2026, 2:49 PM (UTC+2)",
    "Tuesday, Sep 15, 2026, 2:49 PM (UTC+24)",
    "Tuesday, Sep 15, 2026, 2:49 PM (UTC+999999999999999999999)",
])
def test_invalid_timestamp_is_unknown(backend, stamp):
    assert backend["_cursor_cli_iso"](stamp) == ""


@pytest.mark.parametrize(("offset", "expected"), [
    ("UTC", "+00:00"), ("UTC-3:30", "-03:30"), ("UTC+5:30", "+05:30"),
])
def test_timestamp_preserves_offset(backend, offset, expected):
    assert backend["_cursor_cli_iso"](f"Tuesday, Sep 15, 2026, 2:49 PM ({offset})") == f"2026-09-15T14:49:00{expected}"


def test_local_thread_reads_a_home_with_uri_characters(cursor_cli_env):
    home = Path(cursor_cli_env["CURSOR_CLI_HOME"])
    renamed = home.with_name("cursor#?%home")
    home.rename(renamed)
    cursor_cli_env["CURSOR_CLI_HOME"] = str(renamed)
    payload = json.loads(_run(cursor_cli_env, "local", "thread", "one-conv-cursor-cli-fixture",
                              "--source", "cursor-cli", "--json", "--no-mark-read").stdout)
    assert [turn["text"] for turn in payload["turns"]] == ["please fix the widget", "Widget patched."]


def test_local_thread_preserves_messages_with_an_invalid_timestamp(cursor_cli_env):
    db = next(Path(cursor_cli_env["CURSOR_CLI_HOME"]).glob(f"chats/*/{CHAT_ID}/store.db"))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE blobs SET data = replace(CAST(data AS TEXT), 'Sep 15', 'Sep 99') WHERE id = ?",
                     (f"{3:064x}",))
    payload = json.loads(_run(cursor_cli_env, "local", "thread", "one-conv-cursor-cli-fixture",
                              "--source", "cursor-cli", "--json", "--no-mark-read").stdout)
    assert [(turn["ts"], turn["text"]) for turn in payload["turns"]] == [
        ("", "please fix the widget"), ("", "Widget patched."),
    ]
