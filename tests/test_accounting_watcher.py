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
"""Mapped history collection is bounded, idempotent and limited to assigned native workers."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from one_conv.accounting import AccountingError, Ledger, watcher
from one_conv.accounting.watcher import load_mapping, sync_mapping, watch_mapping


@pytest.fixture
def native_rows():
    return [{"type": "session_meta", "payload": {"id": "parent", "model_provider": "openai"}},
            {"type": "turn_context", "payload": {"model": "fixture-model"}},
            {"type": "token_usage_record", "ordinal": 1, "timestamp": "2026-01-01T00:10:00Z",
             "payload": {"response_id": "resp-parent", "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}}}]


@pytest.fixture
def history(tmp_path, native_rows):
    directory = tmp_path / "history"
    directory.mkdir()
    (directory / "parent.jsonl").write_text("".join(json.dumps(row) + "\n" for row in native_rows))
    return directory


@pytest.fixture
def mapping(tmp_path, history):
    path = tmp_path / "workers.json"
    path.write_text(json.dumps({"schema_version": 1,
                               "histories": [{"source": "codex", "path": "history", "billing_scope": "native-account"}],
                               "owners": {"codex:parent": {"cost_center_id": "jack", "worker_id": "node/codex:parent"}}}))
    return path


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger")


def test_mapping_paths_resolve_beside_mapping_file(mapping, history):
    assert load_mapping(mapping)["histories"][0]["path"] == history


def test_owned_native_usage_is_recorded(mapping, ledger):
    assert sync_mapping(mapping, ledger)["recorded"] == 1


def test_repeated_scan_is_duplicate(mapping, ledger):
    sync_mapping(mapping, ledger)
    assert sync_mapping(mapping, ledger)["duplicates"] == 1


def test_new_usage_appended_between_passes_is_imported(mapping, ledger, history, native_rows):
    sync_mapping(mapping, ledger)
    native_rows[-1]["payload"]["response_id"] = "resp-new"
    with (history / "parent.jsonl").open("a") as stream:
        stream.write(json.dumps(native_rows[-1]) + "\n")
    assert sync_mapping(mapping, ledger)["recorded"] == 1


def test_worker_replacement_during_scan_cannot_change_owner(mapping, ledger, history, native_rows, monkeypatch):
    original = watcher._identity
    def replace_after_inspection(path, source):
        identity = original(path, source)
        native_rows[0]["payload"]["id"] = "foreign-worker"
        path.write_text("".join(json.dumps(row) + "\n" for row in native_rows))
        return identity
    monkeypatch.setattr(watcher, "_identity", replace_after_inspection)
    with pytest.raises(AccountingError, match="identity changed"):
        sync_mapping(mapping, ledger)


def test_worker_is_node_qualified(mapping, ledger):
    sync_mapping(mapping, ledger)
    assert ledger.list_events()[0]["worker_id"] == "node/codex:parent"


def test_subscription_cash_is_unknown(mapping, ledger):
    sync_mapping(mapping, ledger)
    assert ledger.list_events()[0]["cash_cost"]["amount"] is None


def test_unmapped_session_is_not_recorded(mapping, ledger, history, native_rows):
    native_rows[0]["payload"]["id"] = "unmapped"
    native_rows[-1]["payload"]["response_id"] = "resp-unmapped"
    (history / "unmapped.jsonl").write_text("".join(json.dumps(row) + "\n" for row in native_rows))
    assert sync_mapping(mapping, ledger)["unmapped_files"] == 1


@pytest.fixture
def child_history(history, native_rows):
    native_rows[0]["payload"] = {"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}}
    native_rows[-1]["payload"]["response_id"] = "resp-child"
    (history / "child.jsonl").write_text("".join(json.dumps(row) + "\n" for row in native_rows))
    return history


def test_native_child_inherits_owner(mapping, ledger, child_history):
    sync_mapping(mapping, ledger)
    assert ledger.summary("jack")["event_count"] == 2


def test_native_child_has_qualified_parent(mapping, ledger, child_history):
    sync_mapping(mapping, ledger)
    child = next(event for event in ledger.list_events() if event["request_id"] == "resp-child")
    assert child["parent_worker_id"] == "node/codex:parent"


def test_native_child_keeps_own_worker_identity(mapping, ledger, child_history):
    sync_mapping(mapping, ledger)
    child = next(event for event in ledger.list_events() if event["request_id"] == "resp-child")
    assert child["worker_id"] == "node/codex:child"


def test_incomplete_append_is_tolerated(mapping, ledger, history):
    with (history / "parent.jsonl").open("a") as stream:
        stream.write('{"type":"token_usage_record","payload":')
    assert sync_mapping(mapping, ledger)["recorded"] == 1


def test_malformed_complete_record_fails(mapping, ledger, history):
    with (history / "parent.jsonl").open("a") as stream:
        stream.write("{broken}\n")
    with pytest.raises(AccountingError, match="Invalid JSON"):
        sync_mapping(mapping, ledger)


@pytest.mark.parametrize("payload", [["invalid"], {"id": "parent", "source": {"subagent": ["invalid"]}}])
def test_malformed_native_identity_is_an_accounting_error(mapping, ledger, history, payload):
    (history / "parent.jsonl").write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
    with pytest.raises(AccountingError, match="Malformed native worker identity"):
        sync_mapping(mapping, ledger)


def test_malformed_identity_enters_watch_backoff(mapping, ledger, history):
    (history / "parent.jsonl").write_text(json.dumps({"type": "session_meta", "payload": ["invalid"]}) + "\n")
    reports = list(watch_mapping(mapping, lambda: ledger, iterations=1))
    assert reports[0]["status"] == "retrying"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Native FIFO checks require POSIX")
def test_fifo_history_cannot_block_bounded_scan(mapping, history):
    os.mkfifo(history / "blocked.jsonl")
    result = subprocess.run([sys.executable, "-O", "-c",
                             "from one_conv.accounting import Ledger; from one_conv.accounting.watcher import sync_mapping; import sys; sync_mapping(sys.argv[1], Ledger(sys.argv[2]), max_seconds=0.05)",
                             str(mapping), str(history / "ledger")], cwd=Path(__file__).parents[1],
                            check=False, capture_output=True, text=True, timeout=3)
    assert "must be regular files" in result.stderr


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Native FIFO checks require POSIX")
def test_fifo_mapping_cannot_block_watcher(tmp_path):
    mapping = tmp_path / "mapping.json"
    os.mkfifo(mapping)
    result = subprocess.run([sys.executable, "-O", "-c",
                             "from one_conv.accounting.watcher import load_mapping; import sys; load_mapping(sys.argv[1])",
                             str(mapping)], cwd=Path(__file__).parents[1], check=False, capture_output=True, text=True, timeout=3)
    assert "must be regular files" in result.stderr


def test_file_limit_is_enforced(mapping, ledger):
    with pytest.raises(AccountingError, match="exceeds 0 files"):
        sync_mapping(mapping, ledger, max_files=0)


def test_event_limit_is_enforced(mapping, ledger):
    with pytest.raises(AccountingError, match="exceeds 0 events"):
        sync_mapping(mapping, ledger, max_events=0)


def test_watch_reloads_ownership_mapping(mapping, ledger):
    passes = watch_mapping(mapping, lambda: ledger, interval=0.1, iterations=2, wait=lambda _: None)
    next(passes)
    data = json.loads(mapping.read_text())
    data["owners"] = {}
    mapping.write_text(json.dumps(data))
    assert next(passes)["unmapped_files"] == 1


def test_watch_backoff_is_bounded(mapping):
    delays = []
    def unavailable():
        raise AccountingError("Fixture authority unavailable")
    list(watch_mapping(mapping, unavailable, interval=1, max_backoff=2, iterations=4, wait=delays.append))
    assert delays == [1, 2, 2]


def test_watch_reload_can_recover_after_outage(mapping, ledger):
    count = 0
    def recover():
        nonlocal count
        count += 1
        if count == 1:
            raise AccountingError("Fixture authority unavailable")
        return ledger
    reports = list(watch_mapping(mapping, recover, interval=0.1, iterations=2, wait=lambda _: None))
    assert reports[-1]["status"] == "synced"
