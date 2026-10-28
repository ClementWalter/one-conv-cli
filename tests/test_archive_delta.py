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
"""Verify archive delta migration, sequence cursors, and HTTP freshness under writes."""

import sqlite3

import pytest
from sqlalchemy import event

from archive.store import Conflict, SQLiteStore
from one_conv.archive import ArchiveError
from test_archive import archive, fixture_env, run, write_claude


@pytest.fixture
def store(tmp_path):
    database = SQLiteStore(f"sqlite:///{tmp_path / 'delta.db'}")
    yield database
    database.engine.dispose()


@pytest.fixture
def legacy_store(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE conversations_v1 (key TEXT PRIMARY KEY, revision TEXT NOT NULL, "
                           "mtime FLOAT NOT NULL, metadata TEXT NOT NULL, search_text TEXT NOT NULL)")
        connection.executemany("INSERT INTO conversations_v1 VALUES (?, ?, ?, ?, ?)",
                               [("z", "old-z", 1, '{"mtime":1}', ""),
                                ("a", "old-a", 2, '{"mtime":2}', "")])
    database = SQLiteStore(f"sqlite:///{path}")
    yield database
    database.engine.dispose()


def test_empty_delta_has_zero_watermark(store):
    assert store.list_changes()["sequence"] == 0


def test_insert_advances_sequence(store):
    store.put("key", "first", {"mtime": 1}, "", {}, None)
    assert store.list_changes()["sequence"] == 1


def test_unchanged_revision_preserves_sequence(store):
    store.put("key", "first", {"mtime": 1}, "", {}, None)
    store.put("key", "first", {"mtime": 1}, "", {}, "first")
    assert store.list_changes()["sequence"] == 1


def test_update_with_older_mtime_is_returned(store):
    store.put("key", "first", {"mtime": 10}, "", {}, None)
    store.put("key", "second", {"mtime": 1}, "", {}, "first")
    assert store.list_changes(since=1)["items"] == [{"key": "key", "revision": "second", "mtime": 1}]


def test_conflict_preserves_sequence(store):
    store.put("key", "first", {"mtime": 1}, "", {}, None)
    try:
        store.put("key", "second", {"mtime": 2}, "", {}, None)
    except Conflict:
        pass
    assert store.list_changes()["sequence"] == 1


def test_failed_commit_rolls_back_sequence(store):
    with store.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_revision BEFORE INSERT ON revisions_v1 "
                                   "BEGIN SELECT RAISE(ABORT, 'reject'); END")
    try:
        store.put("key", "first", {"mtime": 1}, "", {}, None)
    except Conflict:
        pass
    assert store.list_changes()["sequence"] == 0


def test_changes_are_ordered_by_sequence_instead_of_key(store):
    store.put("z", "first", {"mtime": 1}, "", {}, None)
    store.put("a", "second", {"mtime": 2}, "", {}, None)
    assert [row["key"] for row in store.list_changes()["items"]] == ["z", "a"]


def test_more_pages_return_sequence_cursor(store):
    store.put("z", "first", {"mtime": 1}, "", {}, None)
    store.put("a", "second", {"mtime": 2}, "", {}, None)
    assert store.list_changes(limit=1)["next"] == 1


def test_final_page_has_no_cursor(store):
    store.put("z", "first", {"mtime": 1}, "", {}, None)
    store.put("a", "second", {"mtime": 2}, "", {}, None)
    assert store.list_changes(since=1, limit=1)["next"] is None


def test_unchanged_delta_is_empty(store):
    store.put("key", "first", {"mtime": 1}, "", {}, None)
    assert store.list_changes(since=1)["items"] == []


def test_update_between_pages_remains_reachable(store):
    store.put("z", "first", {"mtime": 1}, "", {}, None)
    store.put("a", "second", {"mtime": 2}, "", {}, None)
    first = store.list_changes(limit=1)
    store.put("z", "updated", {"mtime": 3}, "", {}, "first")
    second = store.list_changes(since=first["next"], limit=1)
    final = store.list_changes(since=second["next"], limit=1)
    assert final["items"] == [{"key": "z", "revision": "updated", "mtime": 3}]


def test_legacy_rows_receive_baseline_sequences(legacy_store):
    assert [row["key"] for row in legacy_store.list_changes()["items"]] == ["z", "a"]


def test_sequence_advances_beyond_migrated_rows(legacy_store):
    legacy_store.put("z", "updated", {"mtime": 3}, "", {}, "old-z")
    assert legacy_store.list_changes(since=2)["sequence"] == 3


@pytest.mark.parametrize("field", ["sequence", "generation", "items"])
def test_reopening_preserves_delta_state(legacy_store, field):
    original = legacy_store.list_changes()
    reopened = SQLiteStore(str(legacy_store.engine.url))
    try:
        assert reopened.list_changes()[field] == original[field]
    finally:
        reopened.engine.dispose()


def test_new_database_has_distinct_generation(store, tmp_path):
    replacement = SQLiteStore(f"sqlite:///{tmp_path / 'replacement.db'}")
    try:
        assert replacement.list_changes()["generation"] != store.list_changes()["generation"]
    finally:
        replacement.engine.dispose()


def test_watermark_and_rows_share_a_snapshot(store):
    with store.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    store.put("key", "first", {"mtime": 1}, "", {}, None)
    changed = False

    def write_during_listing(conn, cursor, statement, parameters, context, executemany):
        nonlocal changed
        # A WAL writer can commit between the watermark read and metadata read.
        if not changed and "FROM conversations_v1" in statement:
            changed = True
            store.put("key", "second", {"mtime": 2}, "", {}, "first")

    event.listen(store.engine, "before_cursor_execute", write_during_listing)
    try:
        page = store.list_changes()
    finally:
        event.remove(store.engine, "before_cursor_execute", write_during_listing)
    assert (page["sequence"], page["items"][0]["revision"]) == (1, "first")


def test_http_delta_returns_store_watermark(archive):
    store, client, _ = archive
    store.put("key", "first", {"mtime": 1}, "", {}, None)
    assert client.request("conversations?since=0") == store.list_changes()


def test_http_delta_follows_native_sync(tmp_path, archive):
    _, client, url = archive
    env = fixture_env(tmp_path)
    env.update(ONE_CONV_ARCHIVE_URL=url, ONE_CONV_ARCHIVE_TOKEN="test-secret")
    write_claude(env)
    run(env, "scripts/archive-sync.py", "--source", "claude")
    initial = client.request("conversations?since=0")
    write_claude(env, extra=True)
    run(env, "scripts/archive-sync.py", "--source", "claude")
    assert client.request(f"conversations?since={initial['sequence']}")["items"][0]["summary"]["turns"] == 3


def test_legacy_http_shape_is_unchanged(archive):
    _, client, _ = archive
    assert set(client.request("conversations")) == {"items", "next"}


@pytest.mark.parametrize("query", ["since=-1", "since=1.0", "since=", "since=abc",
                                  "since=9223372036854775808", "since=0&after=", "since=0&query=term"])
def test_http_rejects_invalid_delta_queries(archive, query):
    _, client, _ = archive
    with pytest.raises(ArchiveError, match="422"):
        client.request("conversations?" + query)
