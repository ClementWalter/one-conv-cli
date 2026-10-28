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
"""`live spawn`: resident agents in tmux. tmux and the harnesses are stubbed, so no agent starts."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from one_conv.live import codex, router, spawn
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
    ("cursor", ["agent", "--dangerously-skip-permissions", "--model", "sonnet", "do it"]),
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
        spawn.command("aider", "do it", None, ())


def test_command_trusts_the_folder_for_cursor_by_flag():
    assert spawn.command("cursor", "do it", None, ("--force",), trust_folder=True) == ["agent", "--trust", "--force", "do it"]


def test_command_leaves_claude_trust_to_the_pane():
    assert spawn.command("claude", "do it", None, (), trust_folder=True) == ["claude", "do it"]


def test_preflight_looks_for_cursors_agent_command(monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda name: None if name == "agent" else f"/bin/{name}")
    with pytest.raises(spawn.SpawnError, match="agent is not on PATH"):
        spawn.preflight("cursor")


def test_wait_returns_the_chat_the_cursor_pane_holds_open(monkeypatch, tmp_path):
    stub_tmux(monkeypatch)
    monkeypatch.setattr(spawn, "_pane_pid", lambda name: 100)
    monkeypatch.setattr(spawn, "_process_tree", lambda root: {100, 101})
    monkeypatch.setattr(spawn.cursor, "chat_for_pid", lambda pid: THREAD if pid == 101 else None)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    assert spawn.wait_for_session("cursor", tmp_path, set(), "t", 5) == f"cursor:{THREAD}"


def test_preflight_refuses_a_logged_out_claude(monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))
    with pytest.raises(spawn.SpawnError, match="not logged in"):
        spawn.preflight("claude")


def test_start_hands_the_pane_this_processs_credentials(homes, monkeypatch):
    monkeypatch.setattr(spawn, "_socket_opened", lambda address: True)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    monkeypatch.setattr(spawn, "wait_for_session", lambda *args, **kw: "claude:x")
    started = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: started.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    spawn.start("claude", "do it", homes)
    assert "CLAUDE_CODE_OAUTH_TOKEN=tok" in started[0][:started[0].index("--")]


def test_environment_reads_an_exported_variable(tmp_path):
    (tmp_path / "env.sh").write_text("# credentials\nexport CLAUDE_CODE_OAUTH_TOKEN=tok\n")
    assert spawn.environment(str(tmp_path / "env.sh"))["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_environment_unquotes_a_value(tmp_path):
    (tmp_path / "env.sh").write_text("BOX_ID='bx 1'\n")
    assert spawn.environment(str(tmp_path / "env.sh"))["BOX_ID"] == "bx 1"


def test_environment_file_wins_over_this_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale")
    (tmp_path / "env.sh").write_text("export CLAUDE_CODE_OAUTH_TOKEN=fresh\n")
    assert spawn.environment(str(tmp_path / "env.sh"))["CLAUDE_CODE_OAUTH_TOKEN"] == "fresh"


def test_environment_skips_lines_that_set_nothing(tmp_path, monkeypatch):
    for var in spawn.AUTH_ENV:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "env.sh").write_text("set -e\n\nexport A=1\n")
    assert spawn.environment(str(tmp_path / "env.sh")) == {"A": "1"}


def test_environment_refuses_a_missing_file(tmp_path):
    with pytest.raises(spawn.SpawnError, match="cannot read the spawner's env file"):
        spawn.environment(str(tmp_path / "absent.sh"))


def test_preflight_checks_claude_with_the_given_environment(monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda name: f"/bin/{name}")
    seen = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: seen.append(kw["env"]) or subprocess.CompletedProcess(argv, 0, "", ""))
    spawn.preflight("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
    assert seen[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_start_hands_the_pane_the_given_environment(homes, monkeypatch):
    monkeypatch.setattr(spawn, "_socket_opened", lambda address: True)
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    monkeypatch.setattr(spawn, "wait_for_session", lambda *args, **kw: "claude:x")
    started = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: started.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    spawn.start("claude", "do it", homes, env={"BOX_ID": "bx1"})
    assert started[0][started[0].index("--") - 2:started[0].index("--")] == ["-e", "BOX_ID=bx1"]


def test_start_checks_the_harness_with_the_given_environment(homes, monkeypatch):
    monkeypatch.setattr(spawn, "_socket_opened", lambda address: True)
    checked = []
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: checked.append(env))
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    monkeypatch.setattr(spawn, "wait_for_session", lambda *args, **kw: "claude:x")
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    spawn.start("claude", "do it", homes, env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
    assert checked == [{"CLAUDE_CODE_OAUTH_TOKEN": "tok"}]


def test_start_reads_the_spawner_env_file_by_default(homes, monkeypatch):
    (homes / "env.sh").write_text("export CLAUDE_CODE_OAUTH_TOKEN=box-token\n")
    (homes / "live" / "bridge").mkdir()
    (homes / "live" / "bridge" / "spawn.json").write_text(json.dumps({"harness": "claude", "env_file": str(homes / "env.sh")}))
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(spawn, "_socket_opened", lambda address: True)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    monkeypatch.setattr(spawn, "wait_for_session", lambda *args, **kw: "claude:x")
    started = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: started.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    spawn.start("claude", "do it", homes)
    assert "CLAUDE_CODE_OAUTH_TOKEN=box-token" in started[0][:started[0].index("--")]


@pytest.fixture
def deaf_claude(homes, monkeypatch):
    """Claude Code 2.1.220 registers its session but opens no messaging socket."""
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(spawn, "wait_for_session", lambda *args, **kw: "claude:x")
    monkeypatch.setattr(spawn, "SOCKET_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [LiveSession("claude", "x", str(homes), delivery="none")])
    monkeypatch.setattr(spawn, "claude_version", lambda: "2.1.220 (Claude Code)")
    stopped = []
    monkeypatch.setattr(spawn, "stop", lambda target: stopped.append(target) or {})
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    return stopped


def test_start_refuses_a_claude_session_without_a_messaging_socket(deaf_claude, homes):
    with pytest.raises(spawn.SpawnError, match="without a messaging socket"):
        spawn.start("claude", "do it", homes)


def test_start_stops_a_claude_session_without_a_messaging_socket(deaf_claude, homes):
    with pytest.raises(spawn.SpawnError):
        spawn.start("claude", "do it", homes)
    assert deaf_claude[0].startswith(spawn.SESSION_PREFIX)


def test_start_remembers_the_claude_version_without_a_messaging_socket(deaf_claude, homes):
    with pytest.raises(spawn.SpawnError):
        spawn.start("claude", "do it", homes)
    assert spawn._deaf_versions() == ["2.1.220 (Claude Code)"]


def test_start_keeps_a_claude_session_with_a_messaging_socket(homes, monkeypatch):
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(spawn, "wait_for_session", lambda *args, **kw: "claude:x")
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [LiveSession("claude", "x", str(homes), delivery="socket")])
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    assert spawn.start("claude", "do it", homes).address == "claude:x"


@pytest.fixture
def remembered_deaf(homes, monkeypatch):
    spawn._record_deaf("2.1.220 (Claude Code)")
    monkeypatch.setattr(spawn.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(spawn.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))


def test_preflight_refuses_a_claude_version_known_to_open_no_socket(remembered_deaf, monkeypatch):
    monkeypatch.setattr(spawn, "claude_version", lambda: "2.1.220 (Claude Code)")
    with pytest.raises(spawn.SpawnError, match="opens no messaging socket"):
        spawn.preflight("claude")


def test_preflight_accepts_claude_once_it_was_updated(remembered_deaf, monkeypatch):
    monkeypatch.setattr(spawn, "claude_version", lambda: "2.1.312 (Claude Code)")
    assert spawn.preflight("claude") is None


def test_preflight_needs_tmux(monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda name: None if name == "tmux" else f"/bin/{name}")
    with pytest.raises(spawn.SpawnError, match="needs tmux"):
        spawn.preflight("codex")


def test_preflight_needs_the_omp_extension(homes, monkeypatch):
    monkeypatch.setattr(spawn.shutil, "which", lambda name: f"/bin/{name}")
    with pytest.raises(spawn.SpawnError, match="live extension"):
        spawn.preflight("omp")


def stub_tmux(monkeypatch, alive=True, pane="", owned=lambda session, name: True):
    monkeypatch.setattr(spawn, "_alive", lambda name: alive)
    monkeypatch.setattr(spawn, "screen", lambda name: pane)
    monkeypatch.setattr(spawn, "owned_by_spawn", owned)
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


def test_wait_skips_an_unrelated_agent_that_registered_first_in_that_folder(monkeypatch, tmp_path):
    stub_tmux(monkeypatch, owned=lambda session, name: session.id == "mine")
    sessions = [LiveSession("claude", "unrelated", str(tmp_path)), LiveSession("claude", "mine", str(tmp_path))]
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: sessions)
    assert spawn.wait_for_session("claude", tmp_path, set(), "t", 5) == "claude:mine"


@pytest.fixture
def pane_tree(monkeypatch):
    """A tmux pane whose process 100 started 101, which started 102; 200 runs elsewhere."""
    monkeypatch.setattr(spawn, "_pane_pid", lambda name: 100)
    table = "100 1\n101 100\n102 101\n200 1\n"
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, table, ""))


def test_process_tree_holds_the_descendants(pane_tree):
    assert spawn._process_tree(100) == {100, 101, 102}


def test_a_session_run_by_the_pane_is_owned(pane_tree):
    assert spawn.owned_by_spawn(LiveSession("omp", "x", pid=102), "t")


def test_a_session_run_elsewhere_is_not_owned(pane_tree):
    assert not spawn.owned_by_spawn(LiveSession("claude", "x", pid=200), "t")


def rollout(homes, text: str) -> None:
    folder = homes / "codex" / "sessions" / "2026" / "10" / "15"
    folder.mkdir(parents=True)
    (folder / f"rollout-2026-10-15T00-00-00-{THREAD}.jsonl").write_text(json.dumps({"text": text}) + "\n")


def test_a_codex_thread_whose_prompt_names_the_spawn_is_owned(homes):
    rollout(homes, "do it\n\n(one-conv spawn oneconv-codex-1)")
    assert spawn.owned_by_spawn(LiveSession("codex", THREAD), "oneconv-codex-1")


def test_a_codex_thread_naming_another_spawn_is_not_owned(homes):
    rollout(homes, "do it\n\n(one-conv spawn oneconv-codex-2)")
    assert not spawn.owned_by_spawn(LiveSession("codex", THREAD), "oneconv-codex-1")


def test_start_names_the_spawn_in_a_codex_prompt(homes, monkeypatch):
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    monkeypatch.setattr(spawn, "wait_for_session", lambda *args, **kw: "codex:x")
    started = []
    monkeypatch.setattr(spawn.subprocess, "run",
                        lambda argv, **kw: started.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    spawned = spawn.start("codex", "do it", homes)
    assert started[0][-1] == f"do it\n\n(one-conv spawn {spawned.tmux})"


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


def test_wait_accepts_the_bypass_consent_for_a_bypass_spawn(monkeypatch, tmp_path):
    sent = stub_tmux(monkeypatch, pane="WARNING: Claude Code running in Bypass Permissions mode")
    calls = iter([[], [LiveSession("claude", "new", str(tmp_path))]])
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: next(calls))
    spawn.wait_for_session("claude", tmp_path, set(), "t", 5, accept_bypass=True)
    assert [argv[-1] for argv in sent] == ["Down", "Enter"]


def test_wait_leaves_the_bypass_consent_alone_otherwise(monkeypatch, tmp_path):
    sent = stub_tmux(monkeypatch, pane="WARNING: Claude Code running in Bypass Permissions mode")
    monkeypatch.setattr(spawn.router, "list_all", lambda harness: [])
    with pytest.raises(spawn.SpawnError):
        spawn.wait_for_session("claude", tmp_path, set(), "t", 0.1)
    assert sent == []


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


def tmux_answers(monkeypatch, kill: tuple[int, str], has: tuple[int, str]) -> None:
    """tmux kill-session and has-session each exit with this (status, stderr)."""
    def run(argv, **kw):
        status, stderr = kill if argv[1] == "kill-session" else has
        return subprocess.CompletedProcess(argv, status, "", stderr)
    monkeypatch.setattr(spawn.subprocess, "run", run)


@pytest.mark.parametrize("stderr, state", [
    ("can't find session: oneconv-c", "absent"),
    ("no server running on /private/tmp/tmux-501/default", "absent"),
    ("error connecting to /private/tmp/tmux-501/default (Permission denied)", "unknown"),
])
def test_a_failed_tmux_probe_is_absent_only_when_tmux_says_so(monkeypatch, stderr, state):
    tmux_answers(monkeypatch, kill=(1, ""), has=(1, stderr))
    assert spawn._state("oneconv-c") == state


@pytest.fixture(params=["kill fails, session still there", "kill and probe both cannot reach the server"])
def unkillable(homes, monkeypatch, request):
    """A spawned Codex agent that kill-session did not end, or whose end tmux cannot confirm."""
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": f"codex:{THREAD}"}})
    unreachable = (1, "error connecting to /private/tmp/tmux-501/default (Permission denied)")
    if request.param.startswith("kill fails"):
        tmux_answers(monkeypatch, kill=(1, "denied"), has=(0, ""))
    else:
        tmux_answers(monkeypatch, kill=unreachable, has=unreachable)


def test_stop_accepts_a_session_that_had_already_ended(homes, monkeypatch):
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": "claude:x"}})
    tmux_answers(monkeypatch, kill=(1, "can't find session: oneconv-c"), has=(1, "can't find session: oneconv-c"))
    spawn.stop("claude:x")
    assert spawn._load() == {}


def test_stop_reports_a_session_it_could_not_end(unkillable):
    with pytest.raises(spawn.SpawnError, match="could not end"):
        spawn.stop(f"codex:{THREAD}")


def test_stop_keeps_the_record_of_a_session_it_could_not_end(unkillable):
    with pytest.raises(spawn.SpawnError):
        spawn.stop(f"codex:{THREAD}")
    assert "oneconv-c" in spawn._load()


def test_stop_leaves_a_codex_thread_it_could_not_end_listed(unkillable):
    with pytest.raises(spawn.SpawnError):
        spawn.stop(f"codex:{THREAD}")
    assert THREAD not in codex.stopped_threads()


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
    tmux_answers(monkeypatch, kill=(0, ""), has=(1, "can't find session: oneconv-c"))
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


@pytest.fixture
def ended_thread(homes):
    """A Codex thread ended with `live stop`, whose rollout still exists."""
    rollout(homes, "do it")
    (homes / "live" / "codex-stopped.json").write_text(json.dumps({THREAD: time.time()}))
    return homes


def test_sending_to_an_ended_codex_thread_is_refused_while_its_lock_lingers(ended_thread):
    path = lock(ended_thread)
    old = time.time() - 60
    os.utime(path, (old, old))
    with pytest.raises(router.LiveError, match="ended with `live stop`"):
        router.resolve(f"codex:{THREAD}")


def test_sending_to_an_ended_codex_thread_is_refused_once_its_lock_is_gone(ended_thread):
    with pytest.raises(router.LiveError, match="ended with `live stop`"):
        router.resolve(f"codex:{THREAD}")


def test_a_resumed_codex_thread_is_reachable_again(ended_thread):
    lock(ended_thread)
    os.utime(ended_thread / "codex" / "thread-writer-locks" / f"{THREAD}.lock", (time.time() + 5, time.time() + 5))
    assert router.resolve(f"codex:{THREAD}").address == f"codex:{THREAD}"


@pytest.mark.parametrize("name", ["cursor-cli", "agent", "cursor"])
def test_every_cursor_cli_name_starts_the_agent_command(name):
    from click.testing import CliRunner
    from one_conv.live.cli import build_live_group
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["spawn", name, "hello"])
    assert "in tmux: agent " in result.output


# --- idle agents are retired ------------------------------------------------------------------------

HOUR = 3600.0


@pytest.fixture
def ended(homes, monkeypatch):
    """tmux ends whatever it is told to; records the names it was told to end."""
    names = []

    def run(argv, **kw):
        if argv[1] == "kill-session":
            names.append(argv[-1])
        return subprocess.CompletedProcess(argv, 1, "", "can't find session")
    monkeypatch.setattr(spawn.subprocess, "run", run)
    return names


def idle(address="claude:x"):
    return [LiveSession("claude", address.partition(":")[2], status="idle")]


def test_an_agent_first_seen_idle_starts_its_idle_clock(ended):
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": "claude:x"}})
    spawn.retire_idle(idle(), now=1000.0, idle_hours=24)
    assert spawn._load()["oneconv-c"]["active_at"] == 1000.0


def test_an_agent_idle_past_the_limit_is_ended(ended):
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": "claude:x", "active_at": 0.0}})
    spawn.retire_idle(idle(), now=25 * HOUR, idle_hours=24)
    assert ended == ["oneconv-c"]


def test_an_ended_idle_agent_leaves_the_registry(ended):
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": "claude:x", "active_at": 0.0}})
    spawn.retire_idle(idle(), now=25 * HOUR, idle_hours=24)
    assert spawn._load() == {}


def test_an_agent_idle_within_the_limit_is_kept(ended):
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": "claude:x", "active_at": 0.0}})
    spawn.retire_idle(idle(), now=23 * HOUR, idle_hours=24)
    assert ended == []


@pytest.mark.parametrize("status", ["busy", "waiting"])
def test_an_agent_at_work_or_waiting_on_the_human_resets_its_clock(ended, status):
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": "claude:x", "active_at": 0.0}})
    spawn.retire_idle([LiveSession("claude", "x", status=status)], now=25 * HOUR, idle_hours=24)
    assert spawn._load()["oneconv-c"]["active_at"] == 25 * HOUR


def test_an_agent_not_yet_registered_is_kept(ended):
    spawn._save({"oneconv-c": {"tmux": "oneconv-c", "address": "", "active_at": 0.0}})
    spawn.retire_idle([], now=25 * HOUR, idle_hours=24)
    assert ended == []
