"""`live spawn`: resident agents in tmux. tmux and the harnesses are stubbed, so no agent starts."""

import json
import os
import time
from pathlib import Path

import pytest

from one_conv.live import codex, spawn
from one_conv.live.model import LiveSession
from one_conv.live.spawn_cli import introduction

THREAD = "01a14043-8f91-74f3-8668-76ec80b61266"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, var in (("codex", "CODEX_HOME"), ("live", "ONE_CONV_LIVE_DIR"), ("omp", "OMP_HOME")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    return tmp_path


@pytest.mark.parametrize("harness, expected", [
    ("claude", ["claude", "--dangerously-skip-permissions", "--model", "sonnet", "do it"]),
    ("codex", ["codex", "--dangerously-skip-permissions", "--model", "sonnet", "do it"]),
    ("omp", ["omp", "--dangerously-skip-permissions", "--model=sonnet", "do it"]),
])
def test_command_puts_extra_flags_before_the_prompt(harness, expected):
    assert spawn.command(harness, "do it", "sonnet", ("--dangerously-skip-permissions",)) == expected


def test_command_keeps_a_tricky_prompt_as_one_argument():
    prompt = 'a "b" $(echo PWNED) x;y\n--leading'
    assert spawn.command("codex", prompt, None, ())[-1] == prompt


def test_command_refuses_an_empty_prompt():
    with pytest.raises(spawn.SpawnError, match="first prompt"):
        spawn.command("claude", "  ", None, ())


def test_command_refuses_an_unknown_harness():
    with pytest.raises(spawn.SpawnError, match="cannot spawn"):
        spawn.command("cursor", "do it", None, ())


def test_preflight_needs_tmux(monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda name: None if name == "tmux" else f"/bin/{name}")
    with pytest.raises(spawn.SpawnError, match="needs tmux"):
        spawn.preflight("codex")


def test_preflight_needs_the_omp_extension(homes, monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda name: f"/bin/{name}")
    with pytest.raises(spawn.SpawnError, match="live extension"):
        spawn.preflight("omp")


def stub_tmux(monkeypatch, alive=True, pane=""):
    monkeypatch.setattr(spawn, "_alive", lambda name: alive)
    monkeypatch.setattr(spawn, "_pane", lambda name: pane)
    sent = []
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: sent.append(argv))
    monkeypatch.setattr(spawn.time, "sleep", lambda seconds: None)
    return sent


def test_wait_returns_the_new_session_in_that_folder(monkeypatch, tmp_path):
    stub_tmux(monkeypatch)
    sessions = [LiveSession("codex", "old", str(tmp_path)), LiveSession("codex", "new", str(tmp_path)),
                LiveSession("codex", "elsewhere", "/other")]
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: sessions)
    assert spawn.wait_for_session("codex", tmp_path, {"codex:old"}, "t", 5) == "codex:new"


def test_wait_refuses_when_the_agent_exits(monkeypatch, tmp_path):
    stub_tmux(monkeypatch, alive=False)
    with pytest.raises(spawn.SpawnError, match="exited before registering"):
        spawn.wait_for_session("codex", tmp_path, set(), "t", 5)


def test_wait_stops_at_claudes_trust_question(monkeypatch, tmp_path):
    stub_tmux(monkeypatch, pane=spawn.TRUST_QUESTION)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    with pytest.raises(spawn.SpawnError, match="--trust-folder"):
        spawn.wait_for_session("claude", tmp_path, set(), "t", 5)


def test_wait_answers_the_trust_question_when_asked(monkeypatch, tmp_path):
    sent = stub_tmux(monkeypatch, pane=spawn.TRUST_QUESTION)
    calls = iter([[], [LiveSession("claude", "new", str(tmp_path))]])
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: next(calls))
    spawn.wait_for_session("claude", tmp_path, set(), "t", 5, trust_folder=True)
    assert [argv[-1] for argv in sent] == ["Down", "Enter"]


def test_wait_gives_up_after_the_deadline(monkeypatch, tmp_path):
    stub_tmux(monkeypatch)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    with pytest.raises(spawn.SpawnError, match="did not register"):
        spawn.wait_for_session("codex", tmp_path, set(), "t", 0)


def test_listing_drops_finished_sessions(homes, monkeypatch):
    spawn._save({"oneconv-a": {"tmux": "oneconv-a"}, "oneconv-b": {"tmux": "oneconv-b"}})
    monkeypatch.setattr(spawn, "_alive", lambda name: name == "oneconv-a")
    assert [row["tmux"] for row in spawn.listing()] == ["oneconv-a"]


def test_stop_refuses_an_unknown_agent(homes):
    with pytest.raises(spawn.SpawnError, match="no spawned agent"):
        spawn.stop("codex:nobody")


def lock(homes, thread=THREAD) -> Path:
    path = Path(homes / "codex" / "thread-writer-locks" / f"{thread}.lock")
    path.parent.mkdir(exist_ok=True)
    path.touch()
    return path


def test_a_stopped_codex_thread_is_not_listed_while_its_lock_lingers(homes, monkeypatch):
    path = lock(homes)
    old = time.time() - 60
    os.utime(path, (old, old))
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": f"codex:{THREAD}"}})
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: None)
    spawn.stop(f"codex:{THREAD}")
    assert THREAD not in [s.id for s in codex.list_sessions()]


def test_a_stopped_codex_thread_resumed_later_is_listed_again(homes):
    path = lock(homes)
    (homes / "live" / "codex-stopped.json").write_text(json.dumps({THREAD: time.time() - 60}))
    os.utime(path, None)  # Touched after the stop, as a resume would.
    assert THREAD in [s.id for s in codex.list_sessions()]


def test_introduction_names_who_started_the_agent():
    sender = LiveSession("claude", "abc", "/work", "lead")
    assert "claude:abc (lead)" in introduction("do it", sender)


def test_introduction_keeps_the_prompt():
    assert introduction("do it", None).endswith("do it")
