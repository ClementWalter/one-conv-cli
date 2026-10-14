"""Live relay: session discovery, sender identity, target resolution and per-harness delivery."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from one_conv.live import claude, codex, cursor, install, omp, router
from one_conv.live.cli import build_live_group
from one_conv.live.model import LiveSession, envelope, parse_address

THREAD = "01a13c8f-049a-71b1-b4e6-e0798600ccaa"
CLI = Path(__file__).resolve().parents[1] / "bin" / "one-conv"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("omp", "OMP_HOME"),
                      ("cursor", "ONE_CONV_CURSOR_HOME"), ("live", "ONE_CONV_LIVE_DIR")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    return tmp_path


@pytest.fixture
def dead_pid():
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _claude_record(homes, pid, session_id="c1", **extra):
    (homes / "claude" / "sessions").mkdir(exist_ok=True)
    record = {"pid": pid, "sessionId": session_id, "cwd": "/work/app", "name": "app-01",
              "status": "idle", "messagingSocketPath": "", **extra}
    (homes / "claude" / "sessions" / f"{pid}.json").write_text(json.dumps(record))


def _codex_thread(homes, thread_id=THREAD, name="Fix tests", loaded=True):
    day = homes / "codex" / "sessions" / "2026" / "10" / "14"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-2026-10-14T15-36-10-{thread_id}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": thread_id, "cwd": "/work/api"}}) + "\n")
    with (homes / "codex" / "session_index.jsonl").open("a") as handle:
        handle.write(json.dumps({"id": thread_id, "thread_name": name}) + "\n")
    if loaded:
        (homes / "codex" / "thread-writer-locks").mkdir(exist_ok=True)
        (homes / "codex" / "thread-writer-locks" / f"{thread_id}.lock").touch()


def _omp_record(homes, pid, session_id="o1"):
    (homes / "live" / "omp").mkdir(exist_ok=True)
    (homes / "live" / "omp" / f"{session_id}.json").write_text(
        json.dumps({"pid": pid, "sessionId": session_id, "cwd": "/work/web", "title": "web"}))


@pytest.fixture
def inbox_socket():
    """A UNIX socket standing in for a Claude Code inbox; collects every line received."""
    directory = tempfile.mkdtemp(prefix="oc", dir="/tmp")  # AF_UNIX paths are capped near 104 bytes
    path = os.path.join(directory, "inbox.sock")
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen(1)
    received = []

    def accept():
        connection, _ = server.accept()
        with connection:
            received.append(connection.makefile().readline())

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield path, received, thread
    server.close()
    shutil.rmtree(directory)


# --- addresses and envelope -------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("claude:abc", ("claude", "abc")),
    ("codex:" + THREAD, ("codex", THREAD)),
    ("my project", (None, "my project")),
    ("http://host", (None, "http://host")),
    ("omp:", (None, "omp:")),
])
def test_parse_address(text, expected):
    assert parse_address(text) == expected


def test_envelope_without_sender_is_unknown_by_default():
    assert envelope("hi", None).startswith('<one-conv-message from="unknown">')


def test_envelope_from_terminal_names_the_user():
    assert envelope("hi", None, "user").startswith('<one-conv-message from="user">')


def test_envelope_tells_the_receiver_how_to_reply():
    assert 'to="codex:t1"' in envelope("hi", LiveSession("codex", "t1"))


def test_envelope_without_known_sender_id_has_no_reply_line():
    assert "send_message" not in envelope("hi", LiveSession("cursor", "?unknown"))


# --- Claude Code ------------------------------------------------------------------


def test_claude_lists_running_session(homes):
    _claude_record(homes, os.getpid())
    assert [s.address for s in claude.list_sessions()] == ["claude:c1"]


def test_claude_skips_exited_session(homes, dead_pid):
    _claude_record(homes, dead_pid)
    assert claude.list_sessions() == []


def test_claude_delivers_user_turn_to_inbox_socket(inbox_socket):
    path, received, thread = inbox_socket
    claude.deliver(LiveSession("claude", "c1", endpoint=path), "hello")
    thread.join(5)
    assert json.loads(received[0])["message"] == {"role": "user", "content": "hello"}


def test_claude_without_socket_refuses():
    with pytest.raises(RuntimeError):
        claude.deliver(LiveSession("claude", "c1"), "hello")


# --- Codex --------------------------------------------------------------------------


def test_codex_lists_loaded_thread_with_name(homes):
    _codex_thread(homes)
    assert [(s.address, s.title, s.cwd) for s in codex.list_sessions()] == [
        (f"codex:{THREAD}", "Fix tests", "/work/api")]


def test_codex_ignores_bookkeeping_locks(homes):
    (homes / "codex" / "thread-writer-locks").mkdir()
    (homes / "codex" / "thread-writer-locks" / ".coordination.lock").touch()
    assert codex.list_sessions() == []


@pytest.fixture
def run_stub(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "Queued message", "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_codex_delivers_through_codex_queue(run_stub):
    codex.deliver(LiveSession("codex", THREAD, status="loaded"), "hello")
    assert run_stub[0][0] == ["codex", "queue", "--thread", THREAD, "--message", "hello"]


def test_codex_reports_queue_for_unloaded_thread(run_stub):
    status = codex.deliver(LiveSession("codex", THREAD, status="not loaded"), "hello")
    assert status == "queued until the thread is resumed"


# --- Oh My Pi -------------------------------------------------------------------------


def test_omp_delivery_drops_message_in_session_inbox(homes):
    omp.deliver(LiveSession("omp", "o1"), "hello")
    [message] = (homes / "live" / "inbox" / "omp" / "o1").glob("*.json")
    assert json.loads(message.read_text()) == {"text": "hello"}


def test_omp_lists_registered_running_session(homes):
    _omp_record(homes, os.getpid())
    assert [s.address for s in omp.list_sessions()] == ["omp:o1"]


def test_omp_skips_exited_session(homes, dead_pid):
    _omp_record(homes, dead_pid)
    assert omp.list_sessions() == []


def test_omp_extension_points_at_live_dir(homes):
    assert json.dumps(str(homes / "live")) in omp.render_extension(["one-conv"])


# --- Cursor ---------------------------------------------------------------------------------


def _hook(conversation_id="k1"):
    return cursor.run_stop_hook({"conversation_id": conversation_id, "workspace_roots": ["/work/ui"],
                                 "status": "completed", "loop_count": 0})


def test_cursor_stop_hook_returns_queued_messages_as_followup(homes):
    _hook()
    for text in ("one", "two"):
        cursor.deliver(cursor.list_sessions()[0], text)
    assert _hook() == {"followup_message": "one\n\ntwo"}


def test_cursor_stop_hook_hands_each_message_over_once(homes):
    _hook()
    cursor.deliver(cursor.list_sessions()[0], "one")
    _hook()
    assert _hook() == {}


def test_cursor_stop_hook_registers_chat(homes):
    _hook()
    assert [(s.address, s.cwd) for s in cursor.list_sessions()] == [("cursor:k1", "/work/ui")]


def test_cursor_stop_hook_without_conversation_does_nothing(homes):
    assert cursor.run_stop_hook({}) == {}


# --- routing and identity ---------------------------------------------------------------


def test_resolve_exact_address(homes):
    _claude_record(homes, os.getpid())
    assert router.resolve("claude:c1").cwd == "/work/app"


def test_resolve_title_substring_across_harnesses(homes):
    _claude_record(homes, os.getpid())
    _codex_thread(homes)
    assert router.resolve("fix tests").address == f"codex:{THREAD}"


def test_resolve_ambiguous_query_lists_candidates(homes):
    _claude_record(homes, os.getpid())
    _omp_record(homes, os.getpid())
    with pytest.raises(router.LiveError, match="several"):
        router.resolve("/work")


def test_resolve_skips_the_sender_before_matching(homes):
    _claude_record(homes, os.getpid(), "c1")
    _claude_record(homes, os.getppid(), "c2")
    assert router.resolve("/work/app", exclude=LiveSession("claude", "c1")).id == "c2"


def test_send_stops_a_runaway_pair(homes):
    _omp_record(homes, os.getpid())
    for _ in range(router.RATE_LIMIT):
        router.send("omp:o1", "ping", LiveSession("claude", "c1"))
    with pytest.raises(router.LiveError, match="relay forever"):
        router.send("omp:o1", "ping", LiveSession("claude", "c1"))


def test_resolve_closed_claude_session_points_to_local_send(homes):
    with pytest.raises(router.LiveError, match="local send"):
        router.resolve("claude:gone")


def test_resolve_unloaded_codex_thread_still_accepts_queue(homes):
    _codex_thread(homes, loaded=False)
    assert router.resolve(f"codex:{THREAD}").status == "not loaded"


def test_send_refuses_the_sending_session_itself(homes):
    _claude_record(homes, os.getpid())
    with pytest.raises(router.LiveError, match="itself"):
        router.send("claude:c1", "hi", LiveSession("claude", "c1"))


def test_send_wraps_message_with_sender(homes):
    _omp_record(homes, os.getpid())
    router.send("omp:o1", "hi", LiveSession("claude", "c1"))
    [message] = (homes / "live" / "inbox" / "omp" / "o1").glob("*.json")
    assert 'from="claude:c1"' in json.loads(message.read_text())["text"]


def test_whoami_explicit_address(homes):
    assert router.whoami("omp:o9").address == "omp:o9"


def test_whoami_rejects_malformed_explicit_address(homes):
    with pytest.raises(router.LiveError):
        router.whoami("nobody")


def test_whoami_codex_tool_call_meta(homes):
    assert router.whoami(meta={"threadId": THREAD}).address == f"codex:{THREAD}"


def test_whoami_nearest_claude_ancestor(homes, monkeypatch):
    _claude_record(homes, 4242, "c-parent")
    monkeypatch.setattr(router, "_process", lambda pid: {100: (4242, "python3")}.get(pid, (1, "claude")))
    assert router.whoami(pid=100).address == "claude:c-parent"


def test_whoami_nearest_ancestor_wins_over_outer_claude(homes, monkeypatch):
    _claude_record(homes, 4242, "c-outer")
    _omp_record(homes, 777, "o-inner")
    chain = {100: (777, "python3"), 777: (4242, "omp")}
    monkeypatch.setattr(router, "_process", lambda pid: chain.get(pid, (1, "claude")))
    assert router.whoami(pid=100).address == "omp:o-inner"


def test_whoami_codex_shell_uses_thread_env(homes, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    monkeypatch.setattr(router, "_process", lambda pid: {100: (900, "python3")}.get(pid, (1, "codex")))
    assert router.whoami(pid=100).address == f"codex:{THREAD}"


def test_whoami_cursor_cli_chat_from_open_store(homes, monkeypatch):
    monkeypatch.setattr(router, "_process", lambda pid: {100: (900, "python3")}.get(pid, (1, "cursor-agent")))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 0, "p900\nn/Users/u/.cursor/chats/d5d6/b1bf197e-7306/store.db\n", ""))
    assert router.whoami(pid=100).address == "cursor:b1bf197e-7306"


def test_whoami_plain_terminal_is_user(homes, monkeypatch):
    monkeypatch.setattr(router, "_process", lambda pid: (1, "zsh"))
    assert router.whoami(pid=100) is None


# --- installer ----------------------------------------------------------------------------


def _step(summary_part):
    return next(step for step in install.plan(["one-conv"]) if summary_part in step.summary)


def test_install_inbound_setting_keeps_other_settings(homes):
    (homes / "claude" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    _step("crossSessionInbound").apply()
    assert json.loads((homes / "claude" / "settings.json").read_text()) == {
        "theme": "dark", "crossSessionInbound": "accept"}


def test_install_cursor_hook_is_registered(homes):
    _step("stop hook").apply()
    assert json.loads((homes / "cursor" / "hooks.json").read_text())["hooks"]["stop"] == [
        {"command": "one-conv live cursor-hook"}]


def test_install_cursor_hook_replaces_previous_relay_hook(homes):
    (homes / "cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [
        {"command": "/old/one-conv live cursor-hook"}, {"command": "notify"}]}}))
    _step("stop hook").apply()
    assert [e["command"] for e in json.loads((homes / "cursor" / "hooks.json").read_text())["hooks"]["stop"]] == [
        "notify", "one-conv live cursor-hook"]


def test_install_cursor_mcp_entry(homes):
    _step("MCP server to").apply()
    assert json.loads((homes / "cursor" / "mcp.json").read_text())["mcpServers"]["one-conv"] == {
        "command": "one-conv", "args": ["mcp"]}


def test_install_detects_mcp_registered_from_another_checkout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 0, "Command: uv\nArgs: run --script /old/one-conv mcp", ""))
    assert not install._registered(["claude", "mcp", "get", "one-conv"], ["uv", "run", "--script", "/new/one-conv", "mcp"])


def test_install_omp_extension_is_done_after_apply(homes):
    step = _step("relay extension")
    step.apply()
    assert step.done()


# --- CLI ------------------------------------------------------------------------------------


def test_live_send_without_yes_is_a_dry_run(homes, monkeypatch):
    monkeypatch.setattr(router, "_process", lambda pid: (1, "zsh"))
    _omp_record(homes, os.getpid())
    CliRunner().invoke(build_live_group(["one-conv"]), ["send", "omp:o1", "hello"])
    assert not (homes / "live" / "inbox" / "omp" / "o1").exists()


def test_local_send_refuses_a_running_claude_session(homes, tmp_path):
    session_id = "11111111-2222-3333-4444-555555555555"
    project = homes / "claude" / "projects" / "-work-app"
    project.mkdir(parents=True)
    (project / f"{session_id}.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": "/work/app", "sessionId": session_id, "timestamp": "2026-10-14T10:00:00Z",
         "message": {"role": "user", "content": "hi"}}) + "\n")
    _claude_record(homes, os.getpid(), session_id)
    result = subprocess.run([sys.executable, str(CLI), "local", "send", "app", "ping", "--yes"],
                            capture_output=True, text=True, env=dict(os.environ))
    assert "open in a running Claude Code session" in result.stderr
