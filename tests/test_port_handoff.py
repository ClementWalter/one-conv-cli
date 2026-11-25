#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""`local port` seeds: whole when short, first request plus latest turns within a budget when long."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from one_conv import handoff

CLI = Path(__file__).parents[1] / "one_conv_cli.py"

SESSION = "5e55a0f0-port-handoff"


def turns_of(sizes: list[int]) -> list[dict]:
    """Alternating user/assistant turns, one second apart, each `size` characters long."""
    return [{"ts": f"2026-01-01T10:{index // 60:02d}:{index % 60:02d}Z", "role": "user" if index % 2 == 0 else "assistant",
             "text": f"t{index}-" + "x" * max(0, size - len(f"t{index}-"))} for index, size in enumerate(sizes)]


def entry_size(index: int, turns: list[dict]) -> int:
    return len(handoff.turn_label(index, len(turns), turns[index])) + len(turns[index]["text"]) + 2


@pytest.fixture
def long_thread():
    return turns_of([1000] * 60)


@pytest.fixture
def long_plan(long_thread):
    return handoff.select(long_thread, 10_000)


def test_a_short_thread_is_kept_whole():
    assert handoff.select(turns_of([100] * 5), 64_000).kept == (0, 1, 2, 3, 4)


def test_a_short_thread_omits_nothing():
    assert handoff.describe(handoff.select(turns_of([100] * 5), 64_000)) == ""


def test_a_zero_budget_keeps_every_turn(long_thread):
    assert handoff.select(long_thread, 0).kept == tuple(range(60))


def test_a_long_thread_keeps_its_first_request(long_plan):
    assert long_plan.kept[0] == 0


def test_a_long_thread_keeps_a_contiguous_run_of_its_newest_turns(long_plan):
    assert long_plan.kept[1:] == tuple(range(long_plan.kept[1], 60))


def test_a_long_thread_fits_its_budget(long_thread, long_plan):
    assert sum(entry_size(index, long_thread) for index in long_plan.kept) <= 10_000


def test_a_long_thread_fills_its_budget_up_to_one_turn(long_thread, long_plan):
    assert sum(entry_size(index, long_thread) for index in long_plan.kept) > 10_000 - entry_size(long_plan.kept[1] - 1, long_thread)


def test_a_long_thread_cuts_no_turn(long_plan):
    assert (long_plan.head_cut, long_plan.tail_cut) == ({}, {})


def test_a_long_thread_names_the_omitted_turns(long_plan):
    assert handoff.describe(long_plan) == f"Turns 2–{long_plan.kept[1]} of 60 are omitted."


def test_a_single_omitted_turn_is_named_in_the_singular():
    assert handoff.describe(handoff.select(turns_of([100, 500, 100]), 300)) == "Turn 2 of 3 is omitted."


def test_limit_keeps_the_first_request_and_the_last_turns(long_thread):
    assert handoff.select(long_thread, 0, window=3).kept == (0, 57, 58, 59)


def test_limit_still_respects_the_budget(long_thread):
    assert handoff.select(long_thread, 2500, window=3).kept == (0, 59)


@pytest.fixture
def oversized_newest():
    turns = turns_of([100, 100, 50_000])
    turns[-1]["text"] += " the latest state"
    return turns


@pytest.fixture
def oversized_newest_plan(oversized_newest):
    return handoff.select(oversized_newest, 10_000)


def test_an_oversized_newest_turn_is_the_only_recent_turn_kept(oversized_newest_plan):
    assert oversized_newest_plan.kept == (0, 2)


def test_an_oversized_newest_turn_keeps_its_end(oversized_newest, oversized_newest_plan):
    assert handoff.render_turn(2, oversized_newest, oversized_newest_plan).endswith("x the latest state")


def test_an_oversized_newest_turn_says_its_start_was_cut(oversized_newest, oversized_newest_plan):
    assert "characters of this turn omitted …]" in handoff.render_turn(2, oversized_newest, oversized_newest_plan)


def test_an_oversized_newest_turn_fits_the_budget(oversized_newest, oversized_newest_plan):
    rendered = [handoff.render_turn(index, oversized_newest, oversized_newest_plan) for index in oversized_newest_plan.kept]
    assert sum(len(entry) + 1 for entry in rendered) <= 10_000


def test_an_oversized_first_request_takes_at_most_half_the_budget():
    turns = turns_of([50_000, 100, 100])
    assert len(handoff.render_turn(0, turns, handoff.select(turns, 10_000))) <= 5_000


@pytest.fixture
def history(tmp_path):
    """Environment for a CLI process whose only history is one 40-turn Claude Code thread of 2,000-character turns."""
    env = {name: value for name, value in os.environ.items() if not name.startswith(("ONE_CONV_", "AGENT_CONV_"))}
    for name, folder in {"CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex", "CURSOR_USER_DIR": "cursor",
                         "CURSOR_CLI_HOME": "cursor-cli", "OMP_HOME": "omp", "ONE_CONV_AGY_HOME": "agy",
                         "ONE_CONV_STATE_DIR": "state", "AGENT_CONV_STATE_DIR": "state", "ONE_CONV_CLOUD_CACHE": "cloud",
                         "ONE_CONV_CHATGPT_CACHE": "chatgpt", "AGENT_CONV_CHATGPT_CACHE": "chatgpt",
                         "ONE_CONV_LIVE_DIR": "live", "ONE_CONV_SESSION_VAULT": "vault"}.items():
        env[name] = str(tmp_path / folder)
    env["ONE_CONV_NO_UPDATE_CHECK"] = "1"
    path = tmp_path / "claude" / "projects" / "-srv-quartz" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    rows = [{"type": turn["role"], "cwd": "/srv/quartz", "timestamp": turn["ts"],
             "message": {"role": turn["role"], "content": turn["text"]}} for turn in turns_of([2000] * 40)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return env


def one_conv(env: dict, *args: str) -> str:
    """Run the CLI as its own process, so module-level provider paths come from `env`."""
    return subprocess.run([sys.executable, "-O", str(CLI), *args], env=env, capture_output=True, text=True).stdout


@pytest.fixture
def dry_run(history):
    return one_conv(history, "local", "port", "quartz", "--into", "claude", "--budget", "20000")


def test_dry_run_reports_the_omitted_turns(dry_run):
    assert "context: Turns 2–32 of 40 are omitted." in dry_run


def test_dry_run_names_the_thread_command_that_reads_them(dry_run):
    assert f"`one-conv local thread claude:{SESSION} --no-mark-read --around 2026-01-01T10:00:16Z --context 15`" in dry_run


@pytest.fixture
def omitted_read(history, dry_run):
    """The timestamps of the turns the seed's own `local thread` command prints."""
    command = re.search(r"`one-conv (local thread [^`]+)`", dry_run)[1]
    return [turn["ts"] for turn in json.loads(one_conv(history, *shlex.split(command), "--json"))["turns"]]


def test_the_thread_command_reads_every_omitted_turn(omitted_read):
    expected = {turn["ts"] for turn in turns_of([2000] * 40)[1:32]}
    assert expected <= set(omitted_read)


def test_port_accepts_a_live_address(history):
    output = one_conv(history, "local", "port", f"claude:{SESSION}", "--into", "codex", "--budget", "0")
    assert "context: whole thread, nothing omitted" in output
