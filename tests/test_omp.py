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
"""Isolated OMP backend coverage for one-conv.

Each test runs the real CLI against a temp $OMP_HOME so it never
touches the machine's live ~/.omp / ~/.claude / ~/.codex history.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "one_conv_cli.py"

SESSION_ID = "01a0test-0000-0000-0000-000000000001"
CWD = "/tmp/one-conv-omp-fixture"


def _write_session(omp_home: Path) -> Path:
    session_dir = omp_home / "agent" / "sessions" / "--tmp-one-conv-omp-fixture"
    session_dir.mkdir(parents=True)
    path = session_dir / f"2026-08-24T12-00-00-000Z_{SESSION_ID}.jsonl"
    events = [
        {
            "type": "title",
            "v": 1,
            "title": "Fix the widget",
            "updatedAt": "2026-08-24T12:00:00.000Z",
        },
        {
            "type": "session",
            "version": 3,
            "id": SESSION_ID,
            "timestamp": "2026-08-24T12:00:00.000Z",
            "cwd": CWD,
            "title": "Fix the widget",
        },
        {
            "type": "message",
            "id": "m1",
            "timestamp": "2026-08-24T12:00:01.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "please fix the widget"}],
            },
        },
        {
            "type": "message",
            "id": "m2",
            "timestamp": "2026-08-24T12:00:02.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I will edit the file."},
                    {
                        "type": "toolCall",
                        "name": "edit",
                        "arguments": {"path": "widget.ts"},
                    },
                    {"type": "text", "text": "Widget patched."},
                ],
                "usage": {"input": 11, "output": 7, "cacheRead": 99, "cacheWrite": 3},
            },
        },
        {
            "type": "message",
            "id": "m3",
            "timestamp": "2026-08-24T12:00:03.000Z",
            "message": {
                "role": "toolResult",
                "content": [{"type": "text", "text": "ok"}],
            },
        },
        {
            "type": "message",
            "id": "m4",
            "timestamp": "2026-08-24T12:00:04.000Z",
            "message": {
                "role": "developer",
                "content": [{"type": "text", "text": "compaction preamble"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


@pytest.fixture()
def omp_env(tmp_path: Path) -> dict[str, str]:
    omp_home = tmp_path / "omp"
    _write_session(omp_home)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = os.environ.copy()
    env["OMP_HOME"] = str(omp_home)
    env["CLAUDE_CONFIG_DIR"] = str(empty / "claude")
    env["CODEX_HOME"] = str(empty / "codex")
    env["CURSOR_USER_DIR"] = str(empty / "cursor")
    env["AGENT_CONV_STATE_DIR"] = str(tmp_path / "state")
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_chats_lists_the_omp_project(omp_env: dict[str, str]) -> None:
    projects = json.loads(_run(omp_env, "chats", "--source", "omp", "--json").stdout)
    assert projects[0]["cwd"] == CWD


def test_chats_tags_source_omp(omp_env: dict[str, str]) -> None:
    projects = json.loads(_run(omp_env, "chats", "--source", "omp", "--json").stdout)
    assert projects[0]["source"] == "omp"


def test_chats_counts_one_thread(omp_env: dict[str, str]) -> None:
    projects = json.loads(_run(omp_env, "chats", "--source", "omp", "--json").stdout)
    assert projects[0]["threads"] == 1


def test_read_uses_the_stored_omp_title(omp_env: dict[str, str]) -> None:
    threads = json.loads(
        _run(omp_env, "read", "one-conv-omp-fixture", "--source", "omp", "--json").stdout
    )
    assert threads[0]["title"] == "Fix the widget"


def test_read_exposes_the_session_uuid(omp_env: dict[str, str]) -> None:
    threads = json.loads(
        _run(omp_env, "read", "one-conv-omp-fixture", "--source", "omp", "--json").stdout
    )
    assert threads[0]["uuid"] == SESSION_ID


def test_thread_compact_shows_user_and_assistant_prose(omp_env: dict[str, str]) -> None:
    payload = json.loads(
        _run(
            omp_env,
            "thread",
            "one-conv-omp-fixture",
            "--source",
            "omp",
            "--json",
            "--no-mark-read",
        ).stdout
    )
    assert [(t["role"], t["text"]) for t in payload["turns"]] == [
        ("user", "please fix the widget"),
        ("assistant", "Widget patched."),
    ]


def test_thread_raw_includes_thinking(omp_env: dict[str, str]) -> None:
    payload = json.loads(
        _run(
            omp_env,
            "thread",
            "one-conv-omp-fixture",
            "--source",
            "omp",
            "--json",
            "--raw",
            "--no-mark-read",
        ).stdout
    )
    joined = "\n".join(t["text"] for t in payload["turns"])
    assert "[thinking] I will edit the file." in joined


def test_thread_raw_includes_tool_call(omp_env: dict[str, str]) -> None:
    payload = json.loads(
        _run(
            omp_env,
            "thread",
            "one-conv-omp-fixture",
            "--source",
            "omp",
            "--json",
            "--raw",
            "--no-mark-read",
        ).stdout
    )
    joined = "\n".join(t["text"] for t in payload["turns"])
    assert "→ edit(" in joined


def test_token_total_excludes_cache_fields(omp_env: dict[str, str]) -> None:
    threads = json.loads(
        _run(
            omp_env,
            "read",
            "one-conv-omp-fixture",
            "--source",
            "omp",
            "--json",
            "--no-mark-read",
        ).stdout
    )
    assert threads[0]["tokens"] == 18


def test_search_finds_user_text(omp_env: dict[str, str]) -> None:
    hits = json.loads(
        _run(omp_env, "search", "fix the widget", "--source", "omp", "--json").stdout
    )
    assert hits[0]["session"] == SESSION_ID


def test_find_matches_stored_title(omp_env: dict[str, str]) -> None:
    hits = json.loads(_run(omp_env, "find", "widget", "--source", "omp", "--json").stdout)
    assert hits[0]["title"] == "Fix the widget"


def test_developer_compaction_preamble_is_not_a_turn(omp_env: dict[str, str]) -> None:
    payload = json.loads(
        _run(
            omp_env,
            "thread",
            "one-conv-omp-fixture",
            "--source",
            "omp",
            "--json",
            "--raw",
            "--no-mark-read",
        ).stdout
    )
    assert not any("compaction preamble" in t["text"] for t in payload["turns"])
