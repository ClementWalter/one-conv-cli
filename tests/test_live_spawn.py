"""`one-conv live spawn`: the harness command, the reply header, the tmux name and the wait for registration."""

import subprocess

import pytest
from click.testing import CliRunner

from one_conv.live import agy, router, spawn
from one_conv.live.cli import build_live_group
from one_conv.live.model import LiveSession

SENDER = LiveSession("claude", "c1")


@pytest.fixture
def started(monkeypatch):
    """Records tmux starts instead of running them; sessions appear once started."""
    calls = []
    monkeypatch.setattr(spawn, "start", lambda name, cwd, command: calls.append((name, cwd, command)))
    monkeypatch.setattr(spawn, "tmux_taken", lambda name: bool(calls))
    monkeypatch.setattr(router, "whoami", lambda *a, **kw: SENDER)
    return calls


def _sessions(monkeypatch, harness, rows):
    monkeypatch.setattr(router.ADAPTERS[harness], "list_sessions", rows)


def test_claude_launch_preapproves_only_one_conv_tools():
    argv = spawn.launch("claude", "brief", "/w", []).argv
    assert argv[argv.index("--allowedTools") + 1].split(",") == [f"mcp__one-conv__{t}" for t in spawn.TOOLS]


def test_claude_launch_ends_with_the_brief_after_double_dash():
    assert spawn.launch("claude", "-starts with a dash", "/w", []).argv[-2:] == ["--", "-starts with a dash"]


def test_codex_launch_approves_send_message():
    assert 'mcp_servers.one-conv.tools.send_message.approval_mode="approve"' in spawn.launch(
        "codex", "brief", "/w", []).argv


def test_codex_launch_trusts_the_working_folder():
    assert 'projects={"/w"={trust_level="trusted"}}' in spawn.launch("codex", "brief", "/w", []).argv


def test_cursor_launch_trusts_the_workspace_without_running_everything():
    assert spawn.launch("cursor", "brief", "/w", []).argv == ["agent", "--trust", "brief"]


def test_harness_arguments_come_before_the_brief():
    assert spawn.launch("omp", "brief", "/w", ["--model", "opus"]).argv == ["omp", "--model", "opus", "brief"]


@pytest.mark.parametrize(("harness", "mode", "flags"), [
    ("claude", "auto", ["--permission-mode", "auto"]),
    ("claude", "yolo", ["--dangerously-skip-permissions"]),
    ("codex", "auto", ["--approve-for-me"]),
    ("codex", "yolo", ["--dangerously-bypass-approvals-and-sandbox"]),
    ("cursor", "auto", ["--auto-review"]),
    ("cursor", "yolo", ["--yolo"]),
    ("omp", "yolo", ["--approval-mode", "yolo"]),
])
def test_mode_flags_follow_the_harness_name(harness, mode, flags):
    assert spawn.launch(harness, "brief", "/w", [], mode).argv[1:1 + len(flags)] == flags


def test_ask_mode_adds_no_flags():
    assert spawn.launch("omp", "brief", "/w", [], "ask").argv == ["omp", "brief"]


def test_mode_flags_come_before_harness_arguments():
    assert spawn.launch("omp", "brief", "/w", ["--model", "opus"], "yolo").argv == [
        "omp", "--approval-mode", "yolo", "--model", "opus", "brief"]


def test_agy_auto_mode_uses_its_mode_flag():
    assert spawn.mode_flags("agy", "auto") == ["--mode", "auto"]


def test_omp_has_no_auto_mode():
    with pytest.raises(ValueError, match="omp has no auto mode"):
        spawn.mode_flags("omp", "auto")


def test_spawn_dry_run_shows_the_mode_flag(started):
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["spawn", "claude", "--mode", "yolo", "--brief", "b"])
    assert "--dangerously-skip-permissions" in result.output


def test_spawn_refuses_a_mode_the_harness_lacks(started):
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["spawn", "omp", "--mode", "auto", "--brief", "b"])
    assert "omp has no auto mode" in result.output


def test_agy_launch_passes_its_language_server_token(monkeypatch):
    monkeypatch.setattr(agy, "launch_command", lambda args: (["agy", "--csrf_token=t", *args], {agy.TOKEN_ENV: "t"}))
    assert spawn.launch("agy", "brief", "/w", []).env == {agy.TOKEN_ENV: "t"}


def test_brief_is_headed_by_the_sender_address():
    assert spawn.with_reply_path("do it", SENDER).startswith("You were started by claude:c1")


def test_brief_from_the_user_has_no_header():
    assert spawn.with_reply_path("do it", None) == "do it"


def test_brief_from_an_unidentified_sender_has_no_header():
    assert spawn.with_reply_path("do it", LiveSession("cursor", "?unknown")) == "do it"


def test_tmux_name_is_harness_and_folder():
    assert spawn.tmux_name("codex", "/w/review.pr-632", lambda name: False) == "codex-review-pr-632"


def test_tmux_name_is_numbered_when_taken():
    assert spawn.tmux_name("codex", "/w/review", lambda name: name == "codex-review") == "codex-review-2"


def test_wait_returns_the_new_session_in_the_folder(tmp_path):
    rows = [LiveSession("codex", "old", str(tmp_path)), LiveSession("codex", "new", str(tmp_path))]
    assert spawn.wait_for(lambda: rows, {"old"}, str(tmp_path), 5, lambda: True).id == "new"


def test_wait_ignores_a_new_session_in_another_folder(tmp_path):
    clock = iter([0, 0, 10]).__next__
    rows = [LiveSession("codex", "new", "/elsewhere")]
    assert spawn.wait_for(lambda: rows, set(), str(tmp_path), 5, lambda: True, sleep=lambda s: None,
                          clock=clock) is None


def test_wait_matches_the_folder_through_a_symlink(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    rows = [LiveSession("claude", "new", str(tmp_path / "real"))]
    assert spawn.wait_for(lambda: rows, set(), str(tmp_path / "link"), 5, lambda: True).id == "new"


def test_wait_stops_once_the_pane_is_gone(tmp_path):
    assert spawn.wait_for(lambda: [], set(), str(tmp_path), 60, lambda: False, sleep=lambda s: None) is None


def test_tmux_start_runs_the_command_without_a_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    spawn.start("s", "/w", spawn.Launch(["codex", "a b; rm -rf ~"], {}))
    assert calls[0][-3:] == ["--", "codex", "a b; rm -rf ~"]


def test_cli_without_yes_only_prints_the_command(started, tmp_path):
    result = CliRunner().invoke(build_live_group(["one-conv"]),
                                ["spawn", "cursor", "--cwd", str(tmp_path), "--brief", "review"])
    assert (result.exit_code, started) == (0, [])


def test_cli_requires_a_brief(started, tmp_path):
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["spawn", "cursor", "--cwd", str(tmp_path), "--yes"])
    assert result.exit_code == 2


def test_cli_prints_the_registered_address(started, monkeypatch, tmp_path):
    _sessions(monkeypatch, "cursor", lambda: [LiveSession("cursor", "k9", str(tmp_path))] if started else [])
    result = CliRunner().invoke(build_live_group(["one-conv"]),
                                ["spawn", "cursor", "--cwd", str(tmp_path), "--brief", "review", "--yes"])
    assert result.output.startswith(f"started cursor:k9 in tmux session cursor-{tmp_path.name}")


def test_cli_passes_arguments_after_double_dash_to_the_harness(started, monkeypatch, tmp_path):
    _sessions(monkeypatch, "omp", lambda: [LiveSession("omp", "o9", str(tmp_path))] if started else [])
    CliRunner().invoke(build_live_group(["one-conv"]),
                       ["spawn", "omp", "--cwd", str(tmp_path), "--brief", "go", "--yes", "--", "--model", "opus"])
    assert started[0][2].argv[1:3] == ["--model", "opus"]


def test_cli_shows_the_screen_when_the_agent_does_not_register(started, monkeypatch, tmp_path):
    _sessions(monkeypatch, "claude", lambda: [])
    monkeypatch.setattr(spawn, "screen", lambda name: "Is this a project you trust?")
    result = CliRunner().invoke(build_live_group(["one-conv"]), [
        "spawn", "claude", "--cwd", str(tmp_path), "--brief", "go", "--yes", "--timeout", "0"])
    assert "Is this a project you trust?" in result.output
