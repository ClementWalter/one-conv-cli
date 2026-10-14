"""Exercise real native readers, HTTP sync and unchanged CLI commands on isolated history."""
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
from one_conv.archive import CHUNK_BYTES, ArchiveClient, ArchiveError, pack

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
    old = client.read_capture(metadata["key"])
    assert old["originals"][str(path)] == path.read_bytes()
    write_claude(env, extra=True)
    assert run(env, "scripts/archive-sync.py", "--source", "claude")["uploaded"] == 1
    updated = run(remote_env, "bin/one-conv", "local", "thread", "ancestry", "--source", "claude", "--no-mark-read", "--json")
    assert updated["turns"][-1]["text"] == "An additional message"
    with pytest.raises(Conflict):
        store.put(pack(old)[0], metadata["revision"])
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
    result = ArchiveClient(url, "test-secret")
    with pytest.raises(ArchiveError, match="422"):
        result.put({"source": "claude"}, {}, None)
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
    run(env, "scripts/archive-sync.py", "--source", "claude")
    archived = path.stat().st_mtime
    older = path.with_name("older-unarchived.jsonl")
    older.write_bytes(path.read_bytes())
    os.utime(older, (archived - 20,) * 2)
    path.write_text(path.read_text().replace("forty-two", "rewritten"))
    os.utime(path, (archived - 10,) * 2)  # Older than the archived capture: stale.
    before = {p: p.read_bytes() for p in (older, path)}
    result = run(env, "scripts/archive-sync.py", "--source", "claude", success=False)
    assert result.returncode == 1
    assert {row["session"] for row in client.list_conversations()} == {"session-one", "older-unarchived"}
    assert "claude/session-one" in result.stderr and "409" in result.stderr and "Conversation changed" in result.stderr
    assert json.loads(result.stdout) == {"uploaded": 1, "unchanged": 0, "failed": 1}
    assert {p: p.read_bytes() for p in before} == before


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
    import copy
    import random
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
    import random
    store = SQLiteStore(f"sqlite:///{tmp_path / 'archive.db'}")
    bound = 2 * CHUNK_BYTES
    url, stop = serve(create_app(store, "test-secret", max_request_bytes=bound))
    try:
        capture = {"origin": "large", "source": "claude", "cwd": "/example/large", "session": "large", "mtime": 1.0,
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
