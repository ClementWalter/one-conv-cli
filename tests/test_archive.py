"""Exercise real native readers, HTTP sync and unchanged CLI commands on isolated history."""
import base64
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading

import pytest
import uvicorn

from archive.server import create_app
from archive.store import SQLiteStore, Conflict
from one_conv.archive import ArchiveClient, ArchiveError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def archive(tmp_path):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'archive.db'}")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(store, "test-secret"), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    yield store, ArchiveClient(url, "test-secret"), url
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()
    store.engine.dispose()


def fixture_env(tmp_path):
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "CODEX_HOME": str(tmp_path / "codex"), "CURSOR_USER_DIR": str(tmp_path / "cursor"),
        "CURSOR_CLI_HOME": str(tmp_path / "cursor-cli"), "OMP_HOME": str(tmp_path / "omp"),
        "ONE_CONV_CORPUS": str(tmp_path / "corpus"), "ONE_CONV_CLOUD_CACHE": str(tmp_path / "cloud"),
        "ONE_CONV_STATE_DIR": str(tmp_path / "state"), "AGENT_CONV_STATE_DIR": str(tmp_path / "state")}
    env.pop("ONE_CONV_ARCHIVE_URL", None)
    env.pop("ONE_CONV_ARCHIVE_TOKEN", None)
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
    old = client.read_conversation(metadata["key"])
    assert base64.b64decode(old["originals"][str(path)]) == path.read_bytes()
    write_claude(env, extra=True)
    assert run(env, "scripts/archive-sync.py", "--source", "claude")["uploaded"] == 1
    updated = run(remote_env, "bin/one-conv", "local", "thread", "ancestry", "--source", "claude", "--no-mark-read", "--json")
    assert updated["turns"][-1]["text"] == "An additional message"
    with pytest.raises(Conflict):
        store.put(old, metadata["revision"])
    assert client.read_conversation(metadata["key"], metadata["revision"]) == old
    assert path.exists()  # The test never removes native data to prove remote reads.
    reopened = SQLiteStore(str(store.engine.url))
    assert len(reopened.read_conversation(metadata["key"])["turns"]) == 3
    reopened.engine.dispose()


def test_auth_failure_and_unavailable_archive_are_visible(tmp_path, archive):
    _, _, url = archive
    with pytest.raises(ArchiveError, match="401"):
        list(ArchiveClient(url, "wrong").list_conversations())
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="wrong")
    result = run(env, "bin/one-conv", "local", "search", "Quartz", "--source", "claude", "--json", success=False)
    assert result.returncode != 0 and "401" in result.stderr
    assert result.stdout == ""
    result = ArchiveClient(url, "test-secret")
    with pytest.raises(ArchiveError, match="422"):
        result.put({"source": "claude"}, None)
    assert list(result.list_conversations()) == []


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
        path = Path(env[native_root]) / "sessions" / "rollout-fixture.jsonl"
        path.parent.mkdir(parents=True)
        events = [{"type": "session_meta", "payload": {"id": "codex-fixture", "cwd": project}},
                  {"type": "response_item", "timestamp": "2026-01-01T10:00:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Quartz question"}]}},
                  {"type": "response_item", "timestamp": "2026-01-01T10:01:00Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Quartz answer"}]}}]
        path.write_text("\n".join(json.dumps(row) for row in events))
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
    assert client.read_conversation(metadata["key"])["originals"]
    env[native_root] = str(tmp_path / "empty")
    for args, expected in zip(commands, baseline):
        assert run(env, "bin/one-conv", namespace, *args, "--source", source, "--json") == expected


def test_interrupted_upload_does_not_commit(tmp_path, archive):
    _, client, url = archive
    from urllib.parse import urlsplit
    target = urlsplit(url)
    with socket.create_connection((target.hostname, target.port)) as connection:
        connection.sendall(b"POST /v1/conversations HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-secret\r\nContent-Length: 200\r\n\r\n{\"document\":")
    assert list(client.list_conversations()) == []


def test_rewrites_and_identity_mismatch_cannot_hide_history(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    document = client.read_conversation(row["key"])
    document["summary"]["uuid"] = "wrong"
    with pytest.raises(ArchiveError, match="422"):
        client.put(document, row["revision"])
    path.write_text(path.read_text().replace("forty-two", "erased"))
    result = run(env, "scripts/archive-sync.py", "--source", "claude", success=False)
    assert result.returncode == 1 and "409" in result.stderr
    assert "forty-two" in client.read_conversation(row["key"])["turns"][1]["blocks"][-1]["text"]


def test_two_origins_are_not_silently_merged_with_native(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    document = client.read_conversation(row["key"])
    document["origin"] = "another-installation"
    document["mtime"] += 1
    client.put(document, None)
    result = run(env, "bin/one-conv", "local", "read", "ancestry", "--source", "claude", "--json", success=False)
    assert result.returncode != 0 and "conflicting origins" in result.stderr
    assert result.stdout == ""


def test_unavailable_archive_is_not_an_empty_result(tmp_path):
    env = fixture_env(tmp_path)
    write_claude(env)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))  # Bound but not listening: connection must fail.
        env.update(ONE_CONV_ARCHIVE_URL=f"http://127.0.0.1:{reserved.getsockname()[1]}", ONE_CONV_ARCHIVE_TOKEN="secret")
        result = run(env, "bin/one-conv", "local", "search", "Quartz", "--source", "claude", "--json", success=False)
        assert result.returncode != 0 and "unavailable" in result.stderr
        assert result.stdout == ""


def test_failed_capture_does_not_starve_older_history(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    path = write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    older = path.with_name("older-unarchived.jsonl")
    older.write_bytes(path.read_bytes())
    os.utime(older, (path.stat().st_mtime - 20,) * 2)
    path.write_text(path.read_text().replace("forty-two", "rewritten"))
    before = {p: p.read_bytes() for p in (older, path)}
    result = run(env, "scripts/archive-sync.py", "--source", "claude", success=False)
    assert result.returncode == 1
    assert {row["session"] for row in client.list_conversations()} == {"session-one", "older-unarchived"}
    assert "claude/session-one" in result.stderr and "409" in result.stderr
    assert json.loads(result.stdout) == {"uploaded": 1, "unchanged": 0, "failed": 1}
    assert {p: p.read_bytes() for p in before} == before


def test_collision_does_not_break_other_sources_or_projects(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    document = client.read_conversation(row["key"])
    document["origin"] = "second-machine"
    client.put(document, None)
    assert run(env, "bin/one-conv", "local", "chats", "--source", "codex", "--json") == []
    document["cwd"] = document["summary"]["cwd"] = "/example/independent"
    client.put(document, None)
    assert len(list(client.list_conversations())) == 3  # Both conflicting captures still exist.
    results = run(env, "bin/one-conv", "local", "search", "Quartz", "--project", "independent", "--source", "claude", "--json")
    assert len(results) == 1 and results[0]["cwd"] == "/example/independent"


def test_revisions_share_unchanged_payload_and_restore_exactly(tmp_path, archive):
    import copy
    import random
    store, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    document = client.read_conversation(row["key"])
    # Incompressible records prevent compression from disguising repeated full copies.
    native = random.Random(42).randbytes(1024 * 1024)
    document["originals"] = {"record": base64.b64encode(native).decode()}
    expected = row["revision"]
    captures = []
    for i in range(10):
        document["mtime"] += 1
        text = f"Appended message {i}"
        document["turns"].append({"ts": "", "role": "user", "blocks": [{"type": "text", "text": text}]})
        document["search_text"].append(text)
        native += text.encode()
        document["originals"]["record"] = base64.b64encode(native).decode()
        expected = client.put(document, expected)["revision"]
        captures.append((expected, copy.deepcopy(document)))
    for digest, capture in captures:
        assert client.read_conversation(row["key"], digest) == capture
    # Ten growing captures should cost near one payload, not ten payloads.
    assert Path(store.engine.url.database).stat().st_size < 3 * 1024 * 1024


def test_existing_full_captures_survive_chunked_updates(tmp_path, archive):
    from one_conv.archive import encode
    store, client, url = archive
    env = fixture_env(tmp_path)
    write_claude(env)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    run(env, "scripts/archive-sync.py", "--source", "claude")
    row = list(client.list_conversations())[0]
    old = client.read_conversation(row["key"])
    # Recreate the full-document row written by the previous archive version.
    with store.engine.begin() as conn:
        conn.execute(store.revisions.update().where(store.revisions.c.key == row["key"]).values(document=encode(old).decode()))
    write_claude(env, extra=True)
    assert run(env, "scripts/archive-sync.py", "--source", "claude")["uploaded"] == 1
    assert client.read_conversation(row["key"], row["revision"]) == old
    assert client.read_conversation(row["key"])["turns"][-1]["blocks"][0]["text"] == "An additional message"
