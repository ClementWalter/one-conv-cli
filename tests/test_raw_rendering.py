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
"""Raw transcript lines: a session read to take it over shows every tool call whole."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

loader = importlib.machinery.SourceFileLoader("agent_conv", str(Path(__file__).parent.parent / "one_conv_cli.py"))
spec = importlib.util.spec_from_loader("agent_conv", loader)
ac = importlib.util.module_from_spec(spec)
sys.modules["agent_conv"] = ac
loader.exec_module(ac)

LONG_COMMAND = "git -C ~/Documents/one-conv-cli worktree add ../one-conv-cli-x -b fix/x origin/main && " + "echo step && " * 40


def assistant(*blocks):
    return ac.Turn(ts="2026-10-26T09:00:00Z", role="assistant", blocks=list(blocks))


@pytest.fixture
def long_call():
    return assistant({"type": "tool_use", "name": "Bash", "input": {"command": LONG_COMMAND}})


def test_a_long_tool_call_is_shown_whole(long_call):
    assert LONG_COMMAND.replace('"', '\\"') in ac._render_turn(long_call, raw=True)


def test_a_short_tool_result_keeps_its_lines():
    turn = assistant({"type": "tool_result", "content": "line one\nline two"})
    assert ac._render_turn(turn, raw=True) == "← line one\nline two"


@pytest.fixture
def huge_result():
    text = "head " + "x" * 10_000 + " the error at the end"
    return ac._render_turn(assistant({"type": "tool_result", "content": text}), raw=True)


def test_a_huge_tool_result_keeps_its_start(huge_result):
    assert huge_result.startswith("← head ")


def test_a_huge_tool_result_keeps_its_end(huge_result):
    assert huge_result.endswith("the error at the end")


def test_a_huge_tool_result_says_what_was_cut(huge_result):
    assert "[… 6026 characters cut …]" in huge_result


def test_compact_reads_still_hide_tool_calls(long_call):
    assert ac._render_turn(long_call, raw=False) is None
