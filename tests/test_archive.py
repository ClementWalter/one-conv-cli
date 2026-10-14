"""Exercise real native readers, HTTP sync and unchanged CLI commands on isolated history."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import zlib
from urllib.parse import urlsplit

import pytest
import uvicorn

from archive.server import create_app
from archive.store import SQLiteStore
from one_conv.archive import CHUNK_BYTES, SYNC_STALE_SECONDS, ArchiveClient, ArchiveError, pack

ROOT = Path(__file__).resolve().parents[1]


def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    def stop():
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    return f"http://127.0.0.1:{sock.getsockname()[1]}", stop


@pytest.fixture
def archive(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'archive.db'}")
    url, stop = serve(create_app(store, "test-secret"))
    yield store, ArchiveClient(url, "test-secret"), url
    stop()
    store.engine.dispose()


def fixture_env(tmp_path):
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "CODEX_HOME": str(tmp_path / "codex"), "CURSOR_USER_DIR": str(tmp_path / "cursor"),
        "CURSOR_CLI_HOME": str(tmp_path / "cursor-cli"), "OMP_HOME": str(tmp_path / "omp"),
        "ONE_CONV_CORPUS": str(tmp_path / "corpus"), "ONE_CONV_CLOUD_CACHE": str(tmp_path / "cloud"),
        "ONE_CONV_STATE_DIR": str(tmp_path / "state"), "AGENT_CONV_STATE_DIR": str(tmp_path / "state")}
    return env


def write_claude(env, extra=False):
    path = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture" / "session-one.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "cwd": "/example/ancestry", "timestamp": "2026-01-01T10:00:00Z", "message": {"role": "user", "content": "Remember Quartz_100% and café?"}},
        {"type": "assistant", "cwd": "/example/ancestry", "timestamp": "2026-01-01T10:01:00Z", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "private reasoning"}, {"type": "tool_use", "name": "example", "input": {"z": 1, "a": 2}}, {"type": "text", "text": "The answer is forty-two."}]}}
    ]
    if extra:
        rows.append({"type": "user", "cwd": "/example/ancestry", "timestamp": "2026-01-01T10:02:00Z", "message": {"role": "user", "content": "An additional message"}})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def write_codex(env):
    path = Path(env["CODEX_HOME"]) / "sessions" / "rollout-fixture.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [{"type": "session_meta", "payload": {"id": "codex-fixture", "cwd": "/example/codex-fixture"}},
              {"type": "response_item", "timestamp": "2026-01-01T10:00:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Quartz question"}]}},
              {"type": "response_item", "timestamp": "2026-01-01T10:01:00Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Quartz answer"}]}}]
    path.write_text("\n".join(json.dumps(row) for row in events))
    return path


def run(env, script, *args, success=True):
    result = subprocess.run([sys.executable, str(ROOT / script), *args], env=env, text=True, capture_output=True)
    if success:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    return result


def test_native_sync_remote_only_parity_and_updates(tmp_path, archive):
    store, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    commands = [("chats",), ("find", "Quartz"), ("read", "ancestry"),
        ("read", "ancestry", "--expand", "--no-mark-read"),
        ("thread", "ancestry", "--no-mark-read"),
        ("thread", "ancestry", "--raw", "--no-mark-read"),
        ("thread", "ancestry", "--include-subagents", "--no-mark-read"),
        ("search", "Quartz_100%"), ("search", "CAFÉ"), ("search", "absent")]
    session_folder = path.with_suffix("")
    (session_folder / "tool-results").mkdir(parents=True)
    (session_folder / "tool-results" / "full-output.txt").write_text("complete tool output")
    (session_folder / "custom-title.json").write_text('{"title": "Ancestry"}')
    baseline = [run(env, "bin/one-conv", "local", *args, "--source", "claude", "--json") for args in commands]
    assert baseline[4]["turns"] == [
        {"ts": "2026-01-01T10:00:00Z", "role": "user", "text": "Remember Quartz_100% and café?"},
        {"ts": "2026-01-01T10:01:00Z", "role": "assistant", "text": "The answer is forty-two."}]
    assert len(baseline[7]) == 1
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 1, "unchanged": 0}
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 0, "unchanged": 1}
    assert run(env, "bin/one-conv", "local", "read", "ancestry", "--source", "claude", "--json") == baseline[2]
    remote_env = {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    for args, expected in zip(commands, baseline):
        assert run(remote_env, "bin/one-conv", "local", *args, "--source", "claude", "--json") == expected
    metadata = list(client.list_conversations())[0]
    old = client.read_capture(metadata["key"])
    assert old["originals"] == {str(p): p.read_bytes() for p in (path, *sorted(session_folder.rglob("*"))) if p.is_file()}
    write_claude(env, extra=True)
    assert run(env, "scripts/archive-sync.py", "--source", "claude")["uploaded"] == 1
    updated = run(remote_env, "bin/one-conv", "local", "thread", "ancestry", "--source", "claude", "--no-mark-read", "--json")
    assert updated["turns"][-1]["text"] == "An additional message"
    with pytest.raises(ArchiveError, match="409"):
        client.put(*pack(old), metadata["revision"])
    assert client.read_capture(metadata["key"], metadata["revision"]) == old
    assert path.exists()  # The test never removes native data to prove remote reads.
    reopened = SQLiteStore(str(store.engine.url))
    assert reopened.read_conversation(metadata["key"]) == client.read_conversation(metadata["key"])
    reopened.engine.dispose()
    assert len(client.read_capture(metadata["key"])["turns"]) == 3


def test_auth_failure_and_unavailable_archive_are_visible(tmp_path, archive):
    _, _, url = archive
    with pytest.raises(ArchiveError, match="401"):
        list(ArchiveClient(url, "wrong").list_conversations())
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="wrong")
    result = run(env, "bin/one-conv", "local", "search", "Quartz", "--source", "claude", "--json", success=False)
    assert result.returncode == 0 and "401" in result.stderr and "native history only" in result.stderr
    assert len(json.loads(result.stdout)) == 1
    client = ArchiveClient(url, "test-secret")
    with pytest.raises(ArchiveError, match="422"):
        client.put({"source": "claude"}, {}, None)
    assert list(client.list_conversations()) == []


@pytest.mark.parametrize("source", ["cursor", "cursor-cli", "omp", "codex", "claude-chat"])
def test_other_sources_keep_native_thread_shape(tmp_path, archive, source):
    _, client, url = archive
    env = fixture_env(tmp_path)
    namespace = "local"
    if source == "cursor":
        project, text, native_root = "/example/cursor-fixture", "Quartz", "CURSOR_USER_DIR"
        path = Path(env[native_root]) / "globalStorage" / "state.vscdb"
        path.parent.mkdir(parents=True)
        workspace = Path(env[native_root]) / "workspaceStorage" / "workspace" / "workspace.json"
        workspace.parent.mkdir(parents=True)
        workspace.write_text(json.dumps({"folder": "file://" + project}))
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE composerHeaders (composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, value)")
            conn.execute("INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?)",
                ("cursor-fixture", "workspace", 1, 2, 0, json.dumps({"name": "Quartz title", "hasUnreadMessages": True})))
            conn.execute("CREATE TABLE cursorDiskKV (key, value)")
            conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", [
                ("composerData:cursor-fixture", json.dumps({"fullConversationHeadersOnly": [{"bubbleId": "one"}]})),
                ("bubbleId:cursor-fixture:one", json.dumps({"type": 1, "text": "Quartz question", "createdAt": "2026-01-01T10:00:00Z"}))])
    elif source == "cursor-cli":
        from test_cursor_cli import _write_chat, CWD
        _write_chat(Path(env["CURSOR_CLI_HOME"]))
        project, text = CWD, "widget"
        native_root = "CURSOR_CLI_HOME"
    elif source == "omp":
        from test_omp import _write_session, CWD
        _write_session(Path(env["OMP_HOME"]))
        project, text = CWD, "widget"
        native_root = "OMP_HOME"
    elif source == "codex":
        project, text, native_root = "/example/codex-fixture", "Quartz", "CODEX_HOME"
        write_codex(env)
    else:
        namespace = "cloud"
        project, text, native_root = "claude-chat:account", "Quartz", "ONE_CONV_CLOUD_CACHE"
        path = Path(env[native_root]) / "account" / "conversation.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"source": source, "cwd": project, "session": "cloud-fixture", "complete": True,
            "turns": [{"role": "user", "ts": "2026-01-01T10:00:00Z", "text": "Quartz question"}]}))
    commands = [("search", text), ("read", project, "--expand", "--raw", "--no-mark-read")]
    if namespace == "cloud":
        commands[1] = ("read", "cloud-fixture", "--raw", "--no-mark-read")
    baseline = [run(env, "bin/one-conv", namespace, *args, "--source", source, "--json") for args in commands]
    assert baseline[0]
    assert (baseline[1] if namespace == "cloud" else baseline[1][0])["turns"]
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    assert run(env, "scripts/archive-sync.py", "--source", source)["uploaded"] == 1
    metadata = list(client.list_conversations())[0]
    originals = client.read_conversation(metadata["key"])["originals"]
    assert set(originals) == {"cursorDiskKV", "composerHeaders"} if source == "cursor" else originals
    env[native_root] = str(tmp_path / "empty")
    for args, expected in zip(commands, baseline):
        assert run(env, "bin/one-conv", namespace, *args, "--source", source, "--json") == expected


def test_interrupted_upload_does_not_commit(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    capture = client.read_capture(list(client.list_conversations())[0]["key"])
    capture["session"] = capture["summary"]["uuid"] = "interrupted"
    manifest, chunks = pack(capture)
    # Every chunk is already stored; only the manifest commit is cut short.
    body = json.dumps({"manifest": manifest, "expected": None}).encode()
    target = urlsplit(url)
    with socket.create_connection((target.hostname, target.port)) as connection:
        connection.sendall(b"POST /v1/conversations HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-secret\r\n"
                           b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body[:len(body) // 2])
    assert [row["session"] for row in client.list_conversations()] == ["session-one"]


def test_rewrites_advance_and_keep_earlier_revisions(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    manifest = client.read_conversation(row["key"])
    manifest["summary"]["uuid"] = "wrong"
    with pytest.raises(ArchiveError, match="422"):
        client.put(manifest, {}, row["revision"])
    # Provider edits, regenerations and parser upgrades all rewrite earlier turns.
    path.write_text(path.read_text().replace("forty-two", "edited"))
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 1, "unchanged": 0}
    assert "edited" in client.read_capture(row["key"])["turns"][1]["blocks"][-1]["text"]
    assert "forty-two" in client.read_capture(row["key"], row["revision"])["turns"][1]["blocks"][-1]["text"]


def test_copies_from_two_installations_show_the_newest(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    # Another machine, or a reset state directory, uploads the same conversation under a new installation ID.
    second = {**env, "ONE_CONV_STATE_DIR": str(tmp_path / "state-two"), "AGENT_CONV_STATE_DIR": str(tmp_path / "state-two")}
    write_claude(env, extra=True)
    assert run(second, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 1, "unchanged": 0}
    assert len(list(client.list_conversations())) == 2
    for reader_env in (env, {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}):
        assert [row["threads"] for row in run(reader_env, "bin/one-conv", "local", "chats", "--source", "claude", "--json")] == [1]
        thread = run(reader_env, "bin/one-conv", "local", "thread", "ancestry", "--source", "claude", "--no-mark-read", "--json")
        assert thread["turns"][-1]["text"] == "An additional message"


def test_sync_skips_captures_whose_source_time_is_unchanged(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    archived = path.stat().st_mtime_ns
    write_claude(env, extra=True)
    os.utime(path, ns=(archived, archived))  # Same modification time: the file is not re-read.
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 0, "unchanged": 1}
    assert len(client.read_capture(list(client.list_conversations())[0]["key"])["turns"]) == 2


def test_unavailable_archive_warns_and_keeps_native_results(tmp_path):
    env = fixture_env(tmp_path)
    write_claude(env)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))  # Bound but not listening: connection must fail.
        env.update(ONE_CONV_ARCHIVE_URL=f"http://127.0.0.1:{reserved.getsockname()[1]}", ONE_CONV_ARCHIVE_TOKEN="secret")
        for args in (("search", "Quartz"), ("chats",), ("thread", "ancestry", "--no-mark-read")):
            result = run(env, "bin/one-conv", "local", *args, "--source", "claude", "--json", success=False)
            assert result.returncode == 0 and "unavailable" in result.stderr and "native history only" in result.stderr
            assert json.loads(result.stdout)


def test_failed_capture_does_not_starve_older_history(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    older = path.with_name("older-unarchived.jsonl")
    older.write_bytes(path.read_bytes())
    os.utime(older, (path.stat().st_mtime - 20,) * 2)
    unreadable = path.with_suffix("") / "tool-results" / "output.txt"  # The newest conversation cannot be captured.
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("complete tool output")
    unreadable.chmod(0)
    try:
        result = run(env, "scripts/archive-sync.py", "--source", "claude", success=False)
    finally:
        unreadable.chmod(0o600)
    assert result.returncode == 1
    assert {row["session"] for row in client.list_conversations()} == {"older-unarchived"}
    assert "claude/session-one" in result.stderr and "PermissionError" in result.stderr
    assert json.loads(result.stdout) == {"uploaded": 1, "unchanged": 0, "failed": 1}


def test_an_older_local_copy_becomes_current(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    first = list(client.list_conversations())[0]
    path.write_text(path.read_text().replace("forty-two", "restored from a backup"))
    os.utime(path, (first["mtime"] - 10,) * 2)
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 1, "unchanged": 0}
    remote = {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    thread = run(remote, "bin/one-conv", "local", "thread", "ancestry", "--source", "claude", "--no-mark-read", "--json")
    assert thread["turns"][-1]["text"] == "The answer is restored from a backup."
    assert "forty-two" in json.dumps(client.read_capture(first["key"], first["revision"])["turns"])


def test_codex_rollouts_sharing_a_session_id_are_archived_separately(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    parent = write_codex(env)
    events = [json.loads(line) for line in parent.read_text().splitlines()]
    events[0]["payload"]["session_id"] = "shared-session"
    parent.write_text("\n".join(json.dumps(event) for event in events))
    subagent = parent.with_name("rollout-subagent.jsonl")
    events[1]["payload"]["content"][0]["text"] = "Subagent question"
    subagent.write_text("\n".join(json.dumps(event) for event in events))
    os.utime(parent, (time.time() - 60,) * 2)
    commands = [("chats",), ("thread", "codex-fixture", "--session", "shared", "--no-mark-read"),
                ("thread", "codex-fixture", "--nth", "2", "--no-mark-read"), ("search", "question")]
    baseline = [run(env, "bin/one-conv", "local", *args, "--source", "codex", "--json") for args in commands]
    assert baseline[1]["turns"][0]["text"] == "Subagent question"  # The newest rollout, as without an archive.
    assert baseline[2]["turns"][0]["text"] == "Quartz question"
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    assert run(env, "scripts/archive-sync.py", "--source", "codex") == {"uploaded": 2, "unchanged": 0}
    assert run(env, "scripts/archive-sync.py", "--source", "codex") == {"uploaded": 0, "unchanged": 2}
    for args, expected in zip(commands, baseline):
        assert run(env, "bin/one-conv", "local", *args, "--source", "codex", "--json") == expected
    remote = {**env, "CODEX_HOME": str(tmp_path / "empty")}
    for args, expected in zip(commands, baseline):
        assert run(remote, "bin/one-conv", "local", *args, "--source", "codex", "--json") == expected


def test_conversations_with_identical_times_keep_one_order(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    stamp = path.stat().st_mtime
    for name in ("f", "c", "e", "a", "d", "b"):
        copy = path.with_name(f"session-{name}.jsonl")
        copy.write_bytes(path.read_bytes())
        os.utime(copy, (stamp, stamp))
    os.utime(path, (stamp, stamp))
    native = run(env, "bin/one-conv", "local", "read", "ancestry", "--source", "claude", "--json")
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    assert run(env, "scripts/archive-sync.py", "--source", "claude")["uploaded"] == 7
    remote = {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    assert run(remote, "bin/one-conv", "local", "read", "ancestry", "--source", "claude", "--json") == native


def test_collision_does_not_break_other_sources_or_projects(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    manifest = client.read_conversation(row["key"])
    manifest["origin"] = "second-machine"
    client.put(manifest, {}, None)
    assert run(env, "bin/one-conv", "local", "chats", "--source", "codex", "--json") == []
    manifest["cwd"] = manifest["summary"]["cwd"] = "/example/independent"
    client.put(manifest, {}, None)
    assert len(list(client.list_conversations())) == 3  # Both conflicting captures still exist.
    results = run(env, "bin/one-conv", "local", "search", "Quartz", "--project", "independent", "--source", "claude", "--json")
    assert len(results) == 1 and results[0]["cwd"] == "/example/independent"


def test_appends_upload_only_changed_tail_and_restore_every_revision(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    capture = client.read_capture(row["key"])
    native = random.Random(42).randbytes(3 * CHUNK_BYTES)  # Incompressible, several chunks long.
    capture["originals"] = {"record": native}
    expected = client.put(*pack(capture), row["revision"])["revision"]
    captures = [(expected, copy.deepcopy(capture))]
    for i in range(3):
        capture["mtime"] += 1
        text = f"Appended message {i}"
        capture["turns"].append({"ts": "", "role": "user", "blocks": [{"type": "text", "text": text}]})
        capture["search_text"].append(text)
        capture["originals"]["record"] += text.encode()
        manifest, chunks = pack(capture)
        missing = client.request("chunks/missing", {"digests": list(chunks)})["missing"]
        # Only the tails of turns, search text and the original record change.
        assert len(missing) == 3 and sum(len(chunks[d]) for d in missing) < CHUNK_BYTES
        expected = client.put(manifest, chunks, expected)["revision"]
        captures.append((expected, copy.deepcopy(capture)))
    for revision, saved in captures:
        assert client.read_capture(row["key"], revision) == saved


def test_capture_larger_than_one_request_round_trips(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'archive.db'}")
    bound = 2 * CHUNK_BYTES
    url, stop = serve(create_app(store, "test-secret", max_request_bytes=bound))
    try:
        capture = {"origin": "large", "source": "claude", "cwd": "/example/large", "session": "large", "locator": "large.jsonl", "mtime": 1.0, "capture_version": "test", "account_label": None,
            "summary": {"uuid": "large", "source": "claude", "cwd": "/example/large", "start": "", "end": "",
                        "turns": 0, "tokens": None, "title": "Large"},
            "unread_native": None, "turns": [], "turns_with_subagents": None, "search_text": ["large session"],
            "originals": {"session.jsonl": random.Random(7).randbytes(3 * bound)}}
        with pytest.raises(ArchiveError, match="413"):
            ArchiveClient(url, "test-secret").put(*pack(capture), None)  # One default-sized batch exceeds this server.
        client = ArchiveClient(url, "test-secret", max_request_bytes=bound)
        receipt = client.put(*pack(capture), None)
        assert client.read_capture(receipt["key"]) == capture
    finally:
        stop()
        store.engine.dispose()


@pytest.mark.parametrize("source", ["claude", "codex"])
def test_archived_trace_uses_existing_local_views(tmp_path, archive, source):
    _, client, url = archive
    env = fixture_env(tmp_path)
    result_text = "tool-result-marker " + "x" * 1000 + " original-tail"
    if source == "claude":
        path = write_claude(env)
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events.append({"type": "user", "cwd": "/example/ancestry", "timestamp": "2026-01-01T10:02:00Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-one", "content": result_text}]}})
        native_root = "CLAUDE_CONFIG_DIR"
        thinking = "private reasoning"
        tool = "example"
    else:
        path = Path(env["CODEX_HOME"]) / "sessions" / "rollout-trace.jsonl"
        path.parent.mkdir(parents=True)
        events = [{"type": "session_meta", "payload": {"id": "trace-fixture", "cwd": "/example/ancestry"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Inspect the file"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "reasoning summary marker"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "read_file", "call_id": "call-one", "arguments": '{"path":"example.txt"}'}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-one", "output": result_text}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Inspection complete"}]}}]
        native_root = "CODEX_HOME"
        thinking = "reasoning summary marker"
        tool = "read_file"
    # This event is deliberately outside the normalized reader's message schema.
    events.append({"type": "provider_metadata", "opaque": {"retained": "unparsed-event-marker"}})
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    commands = [("thread", "ancestry", "--no-mark-read"),
                ("thread", "ancestry", "--raw", "--no-mark-read"),
                ("search", "tool-result-marker")]
    baseline = [run(env, "bin/one-conv", "local", *args, "--source", source, "--json") for args in commands]
    compact = "\n".join(turn["text"] for turn in baseline[0]["turns"])
    raw = "\n".join(turn["text"] for turn in baseline[1]["turns"])
    assert thinking not in compact and "tool-result-marker" not in compact
    assert thinking in raw and tool in raw and "tool-result-marker" in raw
    assert "original-tail" not in raw  # Existing local rendering remains summarized.
    assert baseline[2] == []  # Search keeps its existing prose-only contract.
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", source)
    row = list(client.list_conversations())[0]
    captured = client.read_capture(row["key"])
    assert captured["originals"][str(path)] == path.read_bytes()
    assert any(block.get("content") == result_text for turn in captured["turns"] for block in turn["blocks"])
    env[native_root] = str(tmp_path / "empty")
    for args, expected in zip(commands, baseline):
        assert run(env, "bin/one-conv", "local", *args, "--source", source, "--json") == expected


def test_search_follows_the_newest_copy(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    # Another machine holds a newer, edited copy of the same conversation.
    other = {**fixture_env(tmp_path / "other"), "ONE_CONV_ARCHIVE_URL": url, "ONE_CONV_ARCHIVE_TOKEN": "test-secret"}
    path = write_claude(other)
    path.write_text(path.read_text().replace("Quartz", "Basalt"))
    os.utime(path, (path.stat().st_mtime + 60,) * 2)
    run(other, "scripts/archive-sync.py", "--source", "claude")
    assert run(env, "bin/one-conv", "local", "search", "Quartz", "--source", "claude", "--json") == []
    assert len(run(env, "bin/one-conv", "local", "search", "Basalt", "--source", "claude", "--json")) == 1


def test_project_directories_sharing_a_cwd_list_each_thread_once(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    first = write_claude(env)
    second = first.parent.with_name("fixture-renamed") / "session-two.jsonl"
    second.parent.mkdir()
    second.write_text(first.read_text().replace("forty-two", "forty-three"))
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    threads = run(env, "bin/one-conv", "local", "read", "ancestry", "--source", "claude", "--json")
    assert sorted(thread["uuid"] for thread in threads) == ["session-one", "session-two"]


def test_one_unreadable_archived_conversation_does_not_hide_other_results(tmp_path, archive):
    store, client, url = archive
    env = fixture_env(tmp_path)
    first = write_claude(env)
    first.with_name("session-two.jsonl").write_text(first.read_text().replace("forty-two", "forty-three"))
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    broken = next(row for row in client.list_conversations() if row["session"] == "session-two")
    with store.engine.begin() as conn:
        conn.execute(store.chunks.delete().where(store.chunks.c.digest.in_(client.read_conversation(broken["key"])["fields"]["turns"])))
    remote = {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    result = run(remote, "bin/one-conv", "local", "search", "Quartz", "--source", "claude", "--json", success=False)
    assert result.returncode == 0 and "session-two" in result.stderr and "404" in result.stderr
    assert [row["session"] for row in json.loads(result.stdout)] == ["session-one"]


def test_archived_cloud_conversations_keep_their_account_label(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    path = Path(env["ONE_CONV_CLOUD_CACHE"]) / "account" / "conversation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"source": "claude-chat", "cwd": "claude-chat:acct-1", "session": "cloud-one", "complete": True,
        "account_label": "me@example.com", "turns": [{"role": "user", "ts": "2026-01-01T10:00:00Z", "text": "Quartz question"}]}))
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude-chat")
    env["ONE_CONV_CLOUD_CACHE"] = str(tmp_path / "empty")
    rows = run(env, "bin/one-conv", "cloud", "chats", "--source", "claude-chat", "--json")
    assert [row["account_label"] for row in rows] == ["me@example.com"]


def test_server_rejects_corrupt_chunks_and_manifests_without_chunks(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    content = b"chunk content"
    for data in (zlib.compress(b"other content"), zlib.compress(content) + b"trailing"):
        with pytest.raises(ArchiveError, match="422"):
            client.request("chunks", {"chunks": {hashlib.sha256(content).hexdigest(): base64.b64encode(data).decode()}})
    row = list(client.list_conversations())[0]
    capture = client.read_capture(row["key"])
    capture["turns"].append({"ts": "", "role": "user", "blocks": [{"type": "text", "text": "Never uploaded"}]})
    with pytest.raises(ArchiveError, match="409.*chunks"):
        client.request("conversations", {"manifest": pack(capture)[0], "expected": row["revision"]})


def test_reader_upgrades_recapture_unchanged_sources(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    manifest = client.read_conversation(row["key"])
    manifest["capture_version"] = "an earlier one-conv"
    client.put(manifest, {}, row["revision"])
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 1, "unchanged": 0}


def test_using_one_conv_syncs_in_the_background(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret", ONE_CONV_ARCHIVE_SYNC_MINUTES="15")
    # Any command starts it, including one that never reads the archive, and returns without waiting.
    assert run(env, "bin/one-conv", "cloud", "providers", success=False).returncode == 0
    deadline = time.time() + 60
    while not list(client.list_conversations()) and time.time() < deadline:
        time.sleep(0.5)
    assert [row["session"] for row in client.list_conversations()] == ["session-one"]
    state = Path(env["ONE_CONV_STATE_DIR"]) / "archive-sync.json"
    while "succeeded" not in json.loads(state.read_text()) and time.time() < deadline:
        time.sleep(0.5)
    assert json.loads(state.read_text())["uploaded"] == 1


def test_failing_or_missing_syncs_are_reported_on_every_command(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    unreadable = path.with_suffix("") / "tool-results" / "output.txt"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("complete tool output")
    unreadable.chmod(0)
    try:
        run(env, "scripts/archive-sync.py", "--source", "claude", success=False)
    finally:
        unreadable.chmod(0o600)
    for _ in range(2):
        result = run(env, "bin/one-conv", "local", "chats", "--source", "claude", "--json", success=False)
        assert result.returncode == 0 and "Archive sync at" in result.stderr and "claude/session-one" in result.stderr
    state = Path(env["ONE_CONV_STATE_DIR"]) / "archive-sync.json"
    state.write_text(json.dumps({"requested": time.time() - 2 * 86400, "started": time.time() - 2 * 86400}))
    result = run(env, "bin/one-conv", "local", "chats", "--source", "claude", "--json", success=False)
    assert "has not completed in the last day" not in result.stderr  # Automatic sync is off.
    env["ONE_CONV_ARCHIVE_SYNC_MINUTES"] = str(3 * 24 * 60)  # On, but not yet due again: nothing starts.
    result = run(env, "bin/one-conv", "local", "chats", "--source", "claude", "--json", success=False)
    assert "has not completed in the last day" in result.stderr


@pytest.mark.parametrize("source", ["claude", "codex"])
def test_freeing_deletes_only_verified_files_and_restore_brings_them_back(tmp_path, archive, source):
    _, _, url = archive
    env = fixture_env(tmp_path)
    old = time.time() - 100 * 86400
    if source == "claude":
        path, session, project = write_claude(env), "session", "ancestry"
        folder = path.with_suffix("")
        (folder / "tool-results").mkdir(parents=True)
        (folder / "tool-results" / "output.txt").write_text("complete tool output")
        files = [path, folder / "tool-results" / "output.txt"]
        unsynced, unsynced_text = folder / "tool-results" / "later.txt", "a file the archive has not captured"
    else:
        path, session, project = write_codex(env), "rollout-fixture", "codex-fixture"
        files = [path]
        unsynced, unsynced_text = path, path.read_text() + "\n" + json.dumps({"type": "event_msg", "payload": {"type": "unsynced"}})
    for item in files:
        os.utime(item, (old, old))
    originals = {item: item.read_bytes() for item in files}
    commands = [("thread", project, "--raw", "--no-mark-read"), ("search", "Quartz"), ("chats",)]
    baseline = [run(env, "bin/one-conv", "local", *args, "--source", source, "--json") for args in commands]
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", source)
    unsynced.write_text(unsynced_text)
    os.utime(unsynced, (old, old))  # Unchanged modification time: the sync skips it, so the archive lacks these bytes.
    result = run(env, "scripts/archive-sync.py", "--source", source, "--free-older-than", "30", success=False)
    assert result.returncode == 0 and json.loads(result.stdout)["kept"] == 1 and "differ from the archived capture" in result.stderr
    assert all(item.exists() for item in files)
    if source == "claude":
        unsynced.unlink()
    else:
        path.write_bytes(originals[path])
        os.utime(path, (old, old))
    result = run(env, "scripts/archive-sync.py", "--source", source, "--free-older-than", "30")
    assert result["freed"] == 1 and result["freed_bytes"] == sum(map(len, originals.values()))
    assert not any(item.exists() for item in files) and not path.with_suffix("").exists()
    for args, expected in zip(commands, baseline):
        assert run(env, "bin/one-conv", "local", *args, "--source", source, "--json") == expected
    (Path(env["ONE_CONV_STATE_DIR"]) / "archive-origin").unlink()  # Restore works after the installation ID is lost.
    assert run(env, "scripts/archive-sync.py", "--restore", session) == {"restored": len(files)}
    assert {item: item.read_bytes() for item in originals} == originals and path.stat().st_mtime == old
    for args, expected in zip(commands, baseline):
        assert run(env, "bin/one-conv", "local", *args, "--source", source, "--json") == expected


def test_a_freed_conversation_written_again_does_not_replace_its_archive(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    old = time.time() - 100 * 86400
    os.utime(path, (old, old))
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    assert run(env, "scripts/archive-sync.py", "--source", "claude", "--free-older-than", "30")["freed"] == 1
    shutil.rmtree(env["ONE_CONV_STATE_DIR"])  # The archive, not local state, remembers what was freed here.
    archived = list(client.list_conversations())
    path.write_text(json.dumps({"type": "user", "cwd": "/example/ancestry", "timestamp": "2026-01-02T10:00:00Z",
                                "message": {"role": "user", "content": "Continued after freeing"}}) + "\n")
    result = run(env, "scripts/archive-sync.py", "--source", "claude", success=False)
    assert result.returncode == 1 and f"{path} was freed on" in result.stderr and "--restore session-one" in result.stderr
    assert list(client.list_conversations()) == archived
    result = run(env, "scripts/archive-sync.py", "--restore", "session-one", success=False)
    assert result.returncode == 1 and "exists with different content" in result.stderr
    path.unlink()
    assert run(env, "scripts/archive-sync.py", "--restore", "session-one") == {"restored": 1}
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 1, "unchanged": 0}  # A new installation.


def test_restore_prefers_the_exact_session_over_longer_ids(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    orphan = path.with_name("session-one.orphaned-1786441964419-f1222adc.jsonl")  # Claude Code leaves these.
    orphan.write_bytes(path.read_bytes())
    old = time.time() - 100 * 86400
    for item in (path, orphan):
        os.utime(item, (old, old))
    originals = {item: item.read_bytes() for item in (path, orphan)}
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    assert run(env, "scripts/archive-sync.py", "--source", "claude", "--free-older-than", "30")["freed"] == 2
    assert run(env, "scripts/archive-sync.py", "--restore", "session-one") == {"restored": 1}
    assert path.read_bytes() == originals[path] and not orphan.exists()
    assert run(env, "scripts/archive-sync.py", "--restore", "session-one.orphaned") == {"restored": 1}
    assert {item: item.read_bytes() for item in originals} == originals


@pytest.mark.parametrize("escape", ["outside history", "through a symbolic link"])
def test_restore_writes_only_inside_native_history(tmp_path, archive, escape):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    outside = tmp_path / "outside"
    outside.mkdir()
    if escape == "outside history":
        planted = outside / "planted.jsonl"
    else:
        (path.parent.parent / "linked").symlink_to(outside)
        planted = path.parent.parent / "linked" / "planted.jsonl"
    row = list(client.list_conversations())[0]
    manifest = client.read_conversation(row["key"])
    manifest["originals"][str(planted)] = manifest["originals"][str(path)]
    client.put(manifest, {}, row["revision"])
    path.unlink()
    result = run(env, "scripts/archive-sync.py", "--restore", "session-one", success=False)
    assert result.returncode == 1 and "nothing was restored" in result.stderr
    assert not path.exists() and not any(outside.iterdir())


def test_restore_skips_newer_copies_this_machine_cannot_hold(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    original = path.read_bytes()
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    manifest = client.read_conversation(list(client.list_conversations())[0]["key"])
    elsewhere = {"/another/home/.claude/projects/fixture/" + Path(name).name: parts for name, parts in manifest["originals"].items()}
    client.put({**manifest, "origin": "another-machine", "mtime": manifest["mtime"] + 60, "originals": elsewhere}, {}, None)
    path.unlink()
    assert run(env, "scripts/archive-sync.py", "--restore", "session-one") == {"restored": 1}
    assert path.read_bytes() == original


def test_damaged_state_files_and_settings_do_not_break_commands(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    state = Path(env["ONE_CONV_STATE_DIR"])
    state.mkdir()
    damaged = ("archive-sync.json", "archive-listing.json", "archive-freed.json")
    for name in damaged:
        (state / name).write_text('{"truncated')
    native = run(env, "bin/one-conv", "local", "chats", "--source", "claude", "--json")
    unreachable = {**env, "ONE_CONV_ARCHIVE_URL": "http://127.0.0.1:1", "ONE_CONV_ARCHIVE_TOKEN": "test-secret"}
    result = run(unreachable, "bin/one-conv", "local", "chats", "--source", "claude", "--json", success=False)
    assert result.returncode == 0 and json.loads(result.stdout) == native and "Showing native history only" in result.stderr
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret", ONE_CONV_ARCHIVE_SYNC_MINUTES="soon")
    result = run(env, "bin/one-conv", "local", "chats", "--source", "claude", "--json", success=False)
    assert result.returncode == 0 and json.loads(result.stdout) == native and "Ignoring ONE_CONV_ARCHIVE_SYNC_MINUTES" in result.stderr
    deadline = time.time() + 60  # The invalid interval falls back to the default, so a sync starts and repairs the state.
    while "succeeded" not in (state / "archive-sync.json").read_text() and time.time() < deadline:
        time.sleep(0.5)
    assert json.loads((state / "archive-sync.json").read_text())["uploaded"] == 1
    if os.name == "posix":
        for name in ("archive-sync.json", "archive-listing.json", "archive-sync.log", "archive-sync.lock", "archive-origin"):
            assert not (state / name).exists() or (state / name).stat().st_mode & 0o777 == 0o600, name


def test_only_one_sync_runs_and_a_stopped_sync_does_not_block_later_ones(tmp_path, archive):
    _, _, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    lock = Path(env["ONE_CONV_STATE_DIR"]) / "archive-sync.lock"
    lock.parent.mkdir()
    lock.touch()
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"skipped": "another sync is running"}
    stopped = time.time() - SYNC_STALE_SECONDS - 60
    os.utime(lock, (stopped, stopped))
    assert run(env, "scripts/archive-sync.py", "--source", "claude") == {"uploaded": 1, "unchanged": 0}
    assert not lock.exists()


def test_offline_archive_keeps_archived_conversations_listed(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'archive.db'}")
    url, stop = serve(create_app(store, "test-secret"))
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    remote = {**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "empty")}
    online = run(remote, "bin/one-conv", "local", "chats", "--source", "claude", "--json")
    stop()
    store.engine.dispose()
    result = run(remote, "bin/one-conv", "local", "chats", "--source", "claude", "--json", success=False)
    assert result.returncode == 0 and json.loads(result.stdout) == online and "archive's listing from" in result.stderr
    result = run(remote, "bin/one-conv", "local", "thread", "ancestry", "--source", "claude", "--json", success=False)
    assert result.returncode != 0 and "stored only in the archive, which is unavailable" in result.stderr
