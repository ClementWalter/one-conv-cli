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
#     "numpy==2.5.4",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""relay-worker/scripts/import_archive.py: a Python archive.db copied into the Worker reads back the same.

Runs only with ONE_CONV_TEST_RELAY_WORKER=1, against `wrangler dev --local`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from archive.server import create_app
from archive.store import SQLiteStore
from one_conv.archive import ArchiveClient, pack, revision
from test_live_hub import serve

SCRIPT = Path(__file__).resolve().parents[1] / "relay-worker" / "scripts" / "import_archive.py"


def capture(origin: str, session: str, mtime: float, text: str) -> dict:
    turns = [{"ts": "2026-01-01T10:00:00Z", "role": "user", "text": text}]
    summary = {"uuid": session, "source": "claude", "cwd": "/srv/quartz", "title": text, "start": "2026-01-01T10:00:00Z",
               "end": "2026-01-01T10:00:00Z", "turns": 1, "tokens": None}
    return {"origin": origin, "source": "claude", "cwd": "/srv/quartz", "session": session,
            "locator": f"/srv/claude/{session}.jsonl", "mtime": mtime, "capture_version": "1", "account_label": None,
            "summary": summary, "unread_native": None, "turns": turns, "turns_with_subagents": turns,
            "search_text": [text], "originals": {f"/srv/claude/{session}.jsonl": text.encode() * 100}}


@pytest.fixture
def source(tmp_path):
    """A Python archive.db with two conversations from two nodes, the first at its second revision."""
    store = SQLiteStore(f"sqlite:///{tmp_path / 'archive.db'}")
    url, stop = serve(create_app(store, "secret"))
    client = ArchiveClient(url, "secret")
    first = client.put(*pack(capture("laptop", "one", 1.0, "Quartz café")), None)
    client.put(*pack(capture("laptop", "one", 2.0, "Quartz café, again")), first["revision"])
    client.put(*pack(capture("box", "two", 1.5, "Basalt")), None)
    stop()
    yield store, first
    store.engine.dispose()


def run_import(worker, database: Path) -> dict:
    result = subprocess.run([sys.executable, str(SCRIPT), "--db", str(database), "--url", worker.url], capture_output=True,
                            text=True, check=False, timeout=120, env={**os.environ, "RELAY_ADMIN_TOKEN": worker.admin})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def imported(relay_worker, source):
    """(Python store, the first revision's receipt, a client on the Worker's archive) after one import."""
    store, first = source
    relay_worker.reset()
    run_import(relay_worker, Path(str(store.engine.url.database)))
    return store, first, ArchiveClient(relay_worker.url + "/archive", relay_worker.admin)


def test_the_worker_lists_what_the_python_archive_lists(imported):
    store, _, worker = imported
    assert worker.list_conversations() == store.list_conversations("", None, 1000)["items"]


def test_the_worker_serves_each_head_manifest(imported):
    store, first, worker = imported
    assert worker.read_conversation(first["key"]) == store.read_conversation(first["key"])


def test_the_worker_serves_an_earlier_revision(imported):
    store, first, worker = imported
    assert worker.read_conversation(first["key"], first["revision"]) == store.read_conversation(first["key"], first["revision"])


def test_the_worker_serves_the_chunks(imported):
    _, first, worker = imported
    assert worker.read_capture(first["key"])["originals"] == {"/srv/claude/one.jsonl": b"Quartz caf\xc3\xa9, again" * 100}


def test_the_worker_searches_the_imported_text(imported):
    _, _, worker = imported
    assert [row["session"] for row in worker.list_conversations(query="BASALT")] == ["two"]


def test_the_worker_keeps_the_change_generation(imported):
    store, _, worker = imported
    assert worker.request("conversations?since=0")["generation"] == store.list_changes(0, 1000)["generation"]


def test_a_node_continues_from_the_imported_head(imported, relay_worker):
    store, first, _ = imported
    token = relay_worker.admin_call("POST", "/admin/tokens", {"node": "laptop"})[1]["token"]
    laptop = ArchiveClient(relay_worker.url + "/archive", token)
    head = revision(store.read_conversation(first["key"]))
    assert laptop.put(*pack(capture("laptop", "one", 3.0, "Quartz, third")), head)["key"] == first["key"]


def test_a_second_import_copies_nothing(imported, relay_worker):
    store, _, _ = imported
    summary = run_import(relay_worker, Path(str(store.engine.url.database)))
    assert [summary["chunks"], summary["revisions"], summary["heads"]] == [0, 0, 0]
