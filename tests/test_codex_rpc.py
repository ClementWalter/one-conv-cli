"""Codex runtime presence uses isolated read-only metadata and bounded Unix WebSocket transport."""

import builtins
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from one_conv.live import codex, codex_rpc
from one_conv.live.model import LiveSession

THREAD = "01a13c8f-049a-71b1-b4e6-e0798600ccaa"
CHILD = "01a13c8f-049a-71b1-b4e6-e0798600ccbb"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    return tmp_path


@pytest.fixture
def pid_file(home):
    path = home / "app-server-daemon/daemon.pid"
    path.parent.mkdir()
    path.write_text('{"pid":4242}')
    return path


@pytest.fixture
def metadata():
    return {"thread": {"id": THREAD, "name": None, "cwd": "/workspace/repository", "status": {"type": "active"},
                       "source": "vscode", "preview": "PRIVATE_NATIVE_TRANSCRIPT_MARKER", "turns": []}}


@pytest.mark.parametrize("native,expected", [("active", "busy"), ("idle", "idle"), ("notLoaded", "loaded"),
                                          ("systemError", "loaded"), ("new-native-status", "loaded")])
def test_native_runtime_status_maps_conservatively(metadata, native, expected):
    metadata["thread"]["status"] = {"type": native}
    assert codex_rpc._summary(metadata, THREAD)["status"] == expected


@pytest.mark.parametrize("native,expected", [("active", "busy"), ("idle", "loaded")])
def test_internal_child_idle_does_not_claim_autonomous_availability(metadata, native, expected):
    metadata["thread"].update(status={"type": native}, source={"subAgent": {"thread_spawn": {"agent_path": "/root/reviewer"}}})
    assert codex_rpc._summary(metadata, THREAD)["status"] == expected


def test_native_child_path_supplies_empty_title_context(metadata):
    metadata["thread"]["source"] = {"subAgent": {"thread_spawn": {"agent_path": "/root/reviewer", "agent_role": None}}}
    assert codex_rpc._summary(metadata, THREAD)["title"] == "/root/reviewer"


def test_native_thread_summary_never_projects_conversation_preview(metadata):
    assert "PRIVATE_NATIVE_TRANSCRIPT_MARKER" not in json.dumps(codex_rpc._summary(metadata, THREAD))


def test_thread_response_must_match_requested_identity(metadata):
    with pytest.raises(codex_rpc.ReadUnavailable):
        codex_rpc._summary(metadata, CHILD)


@pytest.mark.parametrize("pid", [None, True, -1, 0, 2**31, "4242"])
def test_daemon_pid_requires_an_actual_positive_integer(pid_file, pid):
    pid_file.write_text(json.dumps({"pid": pid}))
    assert codex_rpc._daemon_pid(pid_file.parents[1]) is None


def test_daemon_pid_symlink_is_not_followed(pid_file):
    target = pid_file.with_name("other.pid")
    pid_file.rename(target)
    pid_file.symlink_to(target)
    assert codex_rpc._daemon_pid(pid_file.parents[1]) is None


def test_deeply_nested_pid_record_is_unavailable(pid_file):
    pid_file.write_text("[" * 2000 + "0" + "]" * 2000)
    assert codex_rpc._daemon_pid(pid_file.parents[1]) is None


def test_missing_temporary_profile_never_discovers_real_daemon(home, monkeypatch):
    monkeypatch.setattr(codex_rpc, "_run", lambda *args: pytest.fail("temporary CODEX_HOME has no daemon record"))
    assert codex_rpc.threads(home) == {}


def test_optional_websocket_dependency_does_not_break_minimal_install(home, monkeypatch):
    original = builtins.__import__

    def optional(name, *args, **kwargs):
        if name.startswith("websockets"):
            raise ImportError("optional WebSocket transport absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", optional)
    assert codex_rpc.threads(home) == {}


def test_pid_fifo_is_rejected_without_blocking(pid_file):
    pid_file.unlink()
    os.mkfifo(pid_file, 0o600)
    script = ("import sys; from pathlib import Path; from one_conv.live.codex_rpc import _daemon_pid; "
              "raise SystemExit(0 if _daemon_pid(Path(sys.argv[1])) is None else 1)")
    process = subprocess.run([sys.executable, "-O", "-c", script, str(pid_file.parents[1])],
                             capture_output=True, text=True, timeout=3, check=False)
    assert process.returncode == 0


@pytest.mark.parametrize("method,params", [("thread/start", {}), ("thread/resume", {"threadId": THREAD}),
                                         ("turn/start", {}), ("config/mcpServer/reload", {}),
                                         ("thread/read", {"threadId": THREAD, "includeTurns": True}),
                                         ("thread/read", {"threadId": "../escape", "includeTurns": False}),
                                         ("thread/loaded/list", {"unexpected": True})])
def test_reader_refuses_mutating_or_history_requests(method, params):
    with pytest.raises(ValueError, match="read-only"):
        codex_rpc.Reader(None, time.monotonic() + 2).call(method, params)


@pytest.fixture
def lock(home):
    path = home / "thread-writer-locks" / (THREAD + ".lock")
    path.parent.mkdir()
    path.touch()
    return path


def test_unavailable_daemon_preserves_loaded_lock_fallback(home, lock, monkeypatch):
    monkeypatch.setattr(codex_rpc, "threads", lambda home: {})
    assert codex.list_sessions()[0].status == "loaded"


def test_runtime_activity_overlays_loaded_lock(home, lock, monkeypatch):
    monkeypatch.setattr(codex_rpc, "threads", lambda home: {THREAD: {"status": "busy", "title": "", "cwd": ""}})
    assert codex.list_sessions()[0].status == "busy"


def test_daemon_only_live_thread_is_listed_without_lock(home, monkeypatch):
    monkeypatch.setattr(codex_rpc, "threads", lambda home: {CHILD: {"status": "busy", "title": "/root/reviewer", "cwd": "/workspace"}})
    assert codex.list_sessions()[0].id == CHILD


def test_explicitly_stopped_thread_is_not_resurrected_by_daemon_memory(home, monkeypatch):
    (home / "live").mkdir()
    (home / "live/codex-stopped.json").write_text(json.dumps({CHILD: time.time()}))
    monkeypatch.setattr(codex_rpc, "threads", lambda home: {CHILD: {"status": "busy", "title": "", "cwd": ""}})
    assert codex.list_sessions() == []


def test_native_context_does_not_replace_existing_user_title(home, lock, monkeypatch):
    (home / "session_index.jsonl").write_text(json.dumps({"id": THREAD, "thread_name": "User's chosen title"}) + "\n")
    monkeypatch.setattr(codex_rpc, "threads", lambda home: {THREAD: {"status": "busy", "title": "/root/reviewer", "cwd": ""}})
    assert codex.list_sessions()[0].title == "User's chosen title"


@pytest.mark.parametrize("status", ["idle", "busy"])
def test_native_runtime_session_still_delivers_to_codex_queue(monkeypatch, status):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "Queued", ""))
    assert codex.deliver(LiveSession("codex", THREAD, status=status), "synthetic message") == "delivered"


@pytest.fixture
def unix_daemon(home, pid_file, metadata, monkeypatch):
    from websockets.sync.server import unix_serve

    calls = []
    behavior = {"thread": metadata["thread"], "notifications": True, "stall": False, "loaded": [THREAD]}
    with tempfile.TemporaryDirectory(prefix="ocx") as directory:
        path = str(Path(directory) / "s")

        def handle(connection):
            try:
                while True:
                    message = json.loads(connection.recv(timeout=2))
                    calls.append(message)
                    method = message["method"]
                    if method == "initialized":
                        continue
                    if behavior["stall"]:
                        time.sleep(0.3)
                        continue
                    if behavior.get("raw_frame"):
                        connection.send(behavior["raw_frame"])
                        continue
                    if behavior["notifications"]:
                        connection.send(json.dumps({"method": "thread/status/changed", "params": {"threadId": THREAD}}))
                    result = ({"userAgent": "isolated daemon"} if method == "initialize" else
                              {"data": behavior["loaded"], "nextCursor": None} if method == "thread/loaded/list" else
                              {"thread": behavior["thread"]})
                    connection.send(json.dumps({"id": message["id"], "result": result}))
            except (OSError, TimeoutError):
                return
            except Exception as error:
                from websockets.exceptions import ConnectionClosed
                if not isinstance(error, ConnectionClosed):
                    raise

        with unix_serve(handle, path=path, close_timeout=0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def discover(argv, deadline):
                return f"{os.getuid()} /portable/bin/codex\n" if argv[0] == "ps" else f"p4242\nn{path}\nn{path}\nn->other\n"

            monkeypatch.setattr(codex_rpc, "_run", discover)
            try:
                yield calls, behavior, path
            finally:
                server.shutdown()
                thread.join(2)


def test_real_unix_websocket_reads_active_runtime(home, unix_daemon):
    assert codex_rpc.threads(home)[THREAD]["status"] == "busy"


def test_real_unix_websocket_uses_only_readonly_methods(home, unix_daemon):
    calls, _, _ = unix_daemon
    codex_rpc.threads(home)
    assert [call["method"] for call in calls] == ["initialize", "initialized", "thread/loaded/list", "thread/read"]


def test_real_unix_websocket_never_requests_turn_contents(home, unix_daemon):
    calls, _, _ = unix_daemon
    codex_rpc.threads(home)
    assert calls[-1]["params"] == {"threadId": THREAD, "includeTurns": False}


def test_real_unix_websocket_projects_no_transcript_preview(home, unix_daemon):
    assert "PRIVATE_NATIVE_TRANSCRIPT_MARKER" not in json.dumps(codex_rpc.threads(home))


def test_real_unix_websocket_has_one_shared_timeout(home, unix_daemon, monkeypatch):
    _, behavior, _ = unix_daemon
    behavior["stall"] = True
    monkeypatch.setattr(codex_rpc, "TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    result = codex_rpc.threads(home)
    assert (result, time.monotonic() - started < 0.5) == ({}, True)


def test_deeply_nested_socket_frame_is_unavailable(home, unix_daemon):
    _, behavior, _ = unix_daemon
    behavior["raw_frame"] = "[" * 2000 + "0" + "]" * 2000
    assert codex_rpc.threads(home) == {}


def test_interleaved_notifications_have_a_frame_limit(home, unix_daemon, monkeypatch):
    monkeypatch.setattr(codex_rpc, "MAX_FRAMES", 1)
    assert codex_rpc.threads(home) == {}


def test_daemon_only_threads_require_native_uuid_identity(home, unix_daemon):
    calls, behavior, _ = unix_daemon
    behavior["loaded"] = ["../escape", "thread_123", "not-a-uuid", THREAD]
    codex_rpc.threads(home)
    assert [call["params"]["threadId"] for call in calls if call["method"] == "thread/read"] == [THREAD]


def test_real_unix_websocket_bounds_thread_reads(home, unix_daemon, monkeypatch):
    calls, behavior, _ = unix_daemon
    behavior["loaded"] = [THREAD, CHILD]
    monkeypatch.setattr(codex_rpc, "MAX_THREADS", 1)
    codex_rpc.threads(home)
    assert len([call for call in calls if call["method"] == "thread/read"]) == 1


def test_real_native_idle_child_remains_unverified(home, unix_daemon):
    _, behavior, _ = unix_daemon
    behavior["thread"].update(status={"type": "idle"}, source={"subAgent": {"thread_spawn": {"agent_path": "/root/reviewer"}}})
    assert codex_rpc.threads(home)[THREAD]["status"] == "loaded"


def test_unresponsive_websocket_handshake_is_bounded(home, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ocx") as directory, socket.socket(socket.AF_UNIX) as server:
        path = str(Path(directory) / "s")
        server.bind(path)
        server.listen(1)

        def accept():
            connection, _ = server.accept()
            with connection:
                time.sleep(0.15)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        monkeypatch.setattr(codex_rpc, "_sockets", lambda *args: [path])
        monkeypatch.setattr(codex_rpc, "TIMEOUT_SECONDS", 0.05)
        started = time.monotonic()
        result = codex_rpc.threads(home)
        elapsed = time.monotonic() - started
        thread.join(1)
    assert (result, elapsed < 0.5) == ({}, True)


def test_discovery_rejects_other_process_executable(home, unix_daemon, monkeypatch):
    monkeypatch.setattr(codex_rpc, "_run", lambda *args: f"{os.getuid()} /portable/bin/python\n")
    assert codex_rpc.threads(home) == {}


def test_discovery_rejects_other_process_owner(home, unix_daemon, monkeypatch):
    monkeypatch.setattr(codex_rpc, "_run", lambda *args: f"{os.getuid() + 1} /portable/bin/codex\n")
    assert codex_rpc.threads(home) == {}


def test_discovery_rejects_regular_files_as_sockets(home, pid_file, tmp_path, monkeypatch):
    target = tmp_path / "not-a-socket"
    target.touch()
    monkeypatch.setattr(codex_rpc, "_run", lambda argv, deadline: f"{os.getuid()} codex\n" if argv[0] == "ps" else f"n{target}\n")
    assert codex_rpc.threads(home) == {}


def test_discovery_rejects_symlinked_sockets(home, unix_daemon, tmp_path, monkeypatch):
    _, _, original = unix_daemon
    target = tmp_path / "socket-link"
    target.symlink_to(original)
    monkeypatch.setattr(codex_rpc, "_run", lambda argv, deadline: f"{os.getuid()} codex\n" if argv[0] == "ps" else f"n{target}\n")
    assert codex_rpc.threads(home) == {}
