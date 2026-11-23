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
"""Isolated Antigravity CLI (agy) backend coverage for one-conv.

Each test runs the real CLI against a temp $ONE_CONV_AGY_HOME so it never
touches the machine's live ~/.gemini / ~/.claude / ~/.codex history.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "one_conv_cli.py"

SESSION_ID = "0a9e7c1d-0000-4000-8000-000000000001"
CWD = "/tmp/one-conv-agy-fixture"

STEPS = [
    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
     "created_at": "2026-11-23T16:30:57Z",
     "content": "<USER_REQUEST>\nplease fix the widget\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n"
                "The current local time is: 2026-11-23T17:30:57+01:00.\n</ADDITIONAL_METADATA>"},
    {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "DONE",
     "created_at": "2026-11-23T16:30:58Z", "error": "No capacity available"},
    {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
     "created_at": "2026-11-23T16:31:02Z", "model": "gemini-3.8-flash-high", "input_tokens": 11,
     "cache_read_tokens": 99, "output_tokens": 7, "thinking": "I will edit the file.",
     "tool_calls": [{"name": "edit_file", "args": {"Path": "widget.ts"}}]},
    {"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-23T16:31:03Z",
     "content": "Created At: 2026-11-23T17:31:03+01:00\nCompleted At: 2026-11-23T17:31:03+01:00\nedited widget.ts"},
    {"step_index": 4, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
     "created_at": "2026-11-23T16:31:04Z",
     "content": "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n<SYSTEM_MESSAGE>\n"
                "<one-conv-message from=\"claude:c1\">run the widget tests</one-conv-message>\n</SYSTEM_MESSAGE>"},
    {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
     "created_at": "2026-11-23T16:31:05Z", "input_tokens": 20, "output_tokens": 5, "content": "Widget patched."},
]


def _write_conversation(agy_home: Path, cwd: str | None = CWD) -> Path:
    logs = agy_home / "brain" / SESSION_ID / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    path = logs / "transcript_full.jsonl"
    path.write_text("".join(json.dumps(step) + "\n" for step in STEPS))
    if cwd:
        with sqlite3.connect(agy_home / "conversation_summaries.db") as conn:
            conn.execute("CREATE TABLE conversation_summaries (conversation_id text, title text, preview text,"
                         " workspace_uris text)")
            conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
                         (SESSION_ID, "Fix the widget", "please fix the widget", json.dumps([f"file://{cwd}"])))
    return path


@pytest.fixture()
def agy_env(tmp_path: Path) -> dict[str, str]:
    _write_conversation(tmp_path / "agy")
    empty = tmp_path / "empty"
    empty.mkdir()
    env = os.environ.copy()
    env["ONE_CONV_AGY_HOME"] = str(tmp_path / "agy")
    env["CLAUDE_CONFIG_DIR"] = str(empty / "claude")
    env["CODEX_HOME"] = str(empty / "codex")
    env["CURSOR_USER_DIR"] = str(empty / "cursor")
    env["OMP_HOME"] = str(empty / "omp")
    env["AGENT_CONV_STATE_DIR"] = str(tmp_path / "state")
    env["ONE_CONV_STATE_DIR"] = str(tmp_path / "state")
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(CLI), *args], check=True, capture_output=True, text=True, env=env)


def _thread(env: dict[str, str], *flags: str) -> dict:
    return json.loads(_run(env, "thread", "one-conv-agy-fixture", "--source", "agy", "--json",
                           "--no-mark-read", *flags).stdout)


def test_chats_lists_the_agy_workspace(agy_env: dict[str, str]) -> None:
    projects = json.loads(_run(agy_env, "chats", "--source", "agy", "--json").stdout)
    assert projects[0]["cwd"] == CWD


def test_chats_tags_source_agy(agy_env: dict[str, str]) -> None:
    projects = json.loads(_run(agy_env, "chats", "--source", "agy", "--json").stdout)
    assert projects[0]["source"] == "agy"


def test_read_uses_the_indexed_agy_title(agy_env: dict[str, str]) -> None:
    threads = json.loads(_run(agy_env, "read", "one-conv-agy-fixture", "--source", "agy", "--json").stdout)
    assert threads[0]["title"] == "Fix the widget"


def test_read_exposes_the_conversation_id(agy_env: dict[str, str]) -> None:
    threads = json.loads(_run(agy_env, "read", "one-conv-agy-fixture", "--source", "agy", "--json").stdout)
    assert threads[0]["uuid"] == SESSION_ID


def test_thread_compact_shows_prompt_relayed_message_and_reply(agy_env: dict[str, str]) -> None:
    assert [(t["role"], t["text"]) for t in _thread(agy_env)["turns"]] == [
        ("user", "please fix the widget"),
        ("user", '<one-conv-message from="claude:c1">run the widget tests</one-conv-message>'),
        ("assistant", "Widget patched."),
    ]


def test_thread_raw_includes_thinking(agy_env: dict[str, str]) -> None:
    joined = "\n".join(t["text"] for t in _thread(agy_env, "--raw")["turns"])
    assert "[thinking] I will edit the file." in joined


def test_thread_raw_includes_the_tool_call(agy_env: dict[str, str]) -> None:
    joined = "\n".join(t["text"] for t in _thread(agy_env, "--raw")["turns"])
    assert "edit_file" in joined


def test_thread_raw_drops_agys_timing_preamble(agy_env: dict[str, str]) -> None:
    joined = "\n".join(t["text"] for t in _thread(agy_env, "--raw")["turns"])
    assert "Created At" not in joined


def test_thread_raw_skips_retried_errors(agy_env: dict[str, str]) -> None:
    joined = "\n".join(t["text"] for t in _thread(agy_env, "--raw")["turns"])
    assert "No capacity" not in joined


def test_thread_names_the_agy_address(agy_env: dict[str, str]) -> None:
    assert _thread(agy_env)["address"].endswith(f"agy:{SESSION_ID}")


def test_tokens_count_input_and_output_only(agy_env: dict[str, str]) -> None:
    threads = json.loads(_run(agy_env, "read", "one-conv-agy-fixture", "--source", "agy", "--json").stdout)
    assert threads[0]["tokens"] == 43


def test_search_finds_text_in_an_agy_conversation(agy_env: dict[str, str]) -> None:
    hits = json.loads(_run(agy_env, "search", "Widget patched", "--source", "agy", "--json", "--offline").stdout)
    assert [hit["session"] for hit in hits] == [SESSION_ID]


def test_find_matches_the_indexed_title(agy_env: dict[str, str]) -> None:
    rows = json.loads(_run(agy_env, "find", "Fix the widget", "--source", "agy", "--json").stdout)
    assert [row["uuid"] for row in rows] == [SESSION_ID]


def test_workspace_falls_back_to_prompt_history(tmp_path: Path, agy_env: dict[str, str]) -> None:
    home = tmp_path / "agy-history-only"
    _write_conversation(home, cwd=None)
    (home / "history.jsonl").write_text(json.dumps({"display": "hi", "workspace": "/srv/history",
                                                    "conversationId": SESSION_ID}) + "\n")
    projects = json.loads(_run({**agy_env, "ONE_CONV_AGY_HOME": str(home)}, "chats", "--source", "agy",
                               "--json").stdout)
    assert projects[0]["cwd"] == "/srv/history"


def test_local_namespace_accepts_agy(agy_env: dict[str, str]) -> None:
    projects = json.loads(_run(agy_env, "local", "chats", "--source", "agy", "--json").stdout)
    assert [p["cwd"] for p in projects] == [CWD]
