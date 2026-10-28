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
"""Accounting clients retain hub authentication boundaries and reject ambiguous acknowledgments."""

import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from one_conv.accounting import client
from one_conv.accounting.client import (
    MAX_BATCH_BYTES,
    AccountingClient,
    configured_client,
)
from one_conv.accounting.model import AccountingError, normalize_event
from one_conv.hr_cli import build_hr_group
from one_conv.live import hubclient


class RecordingHub:
    """A transport stub records requests without credentials or network access."""

    node = "node-a"

    def __init__(self):
        self.calls = []
        self.answer = {"rates": []}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(method, path, body) if callable(self.answer) else copy.deepcopy(self.answer)


@pytest.fixture
def hub():
    return RecordingHub()


@pytest.fixture
def event():
    return {"billing_scope": "example-account", "source": "codex", "source_event_id": "resp-example",
            "request_id": "resp-example", "cost_center_id": "jack", "worker_id": "codex:child",
            "parent_worker_id": "codex:parent", "timestamp": "2026-01-01T00:10:00Z", "provenance": "Native fixture"}


@pytest.fixture
def event_hub(hub):
    hub.answer = lambda method, path, body: {"receipts": [{"status": "recorded", "event": event} for event in body["events"]]}
    return hub


def test_configured_hub_is_default(hub, monkeypatch):
    monkeypatch.setattr(hubclient, "configured", lambda: hub)
    assert configured_client().hub is hub


def test_explicit_local_never_reads_hub_config(monkeypatch):
    monkeypatch.setattr(hubclient, "configured", lambda: pytest.fail("Local mode cannot contact a hub"))
    assert configured_client(False) is None


def test_required_hub_cannot_fall_back(monkeypatch):
    monkeypatch.setattr(hubclient, "configured", lambda: None)
    with pytest.raises(AccountingError, match="No hub configured"):
        configured_client(True)


def test_directory_cannot_silently_change_authority(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(hubclient, "configured", lambda: hub)
    with pytest.raises(AccountingError, match="--local"):
        configured_client(directory=tmp_path)


def test_unavailable_hub_never_becomes_local(hub):
    hub.answer = hubclient.HubUnavailable("Unavailable fixture authority")
    with pytest.raises(AccountingError, match="Unavailable fixture authority"):
        AccountingClient(hub).rates()


def test_usage_filters_are_encoded(hub):
    hub.answer = {"events": []}
    AccountingClient(hub).list_events("jack", since="2026-01-01T00:00:00+01:00", limit=20)
    assert parse_qs(urlsplit(hub.calls[0][1]).query)["since"] == ["2026-01-01T00:00:00+01:00"]


def test_node_worker_is_qualified(event_hub, event):
    AccountingClient(event_hub).record_many([event])
    assert event_hub.calls[0][2]["events"][0]["worker_id"] == "node-a/codex:child"


def test_node_parent_is_qualified(event_hub, event):
    AccountingClient(event_hub).record_many([event])
    assert event_hub.calls[0][2]["events"][0]["parent_worker_id"] == "node-a/codex:parent"


def test_canonical_transport_contains_no_extra_fields(event_hub, event):
    event["text"] = "private fixture content"
    with pytest.raises(AccountingError, match="unsupported"):
        AccountingClient(event_hub).record_many([event])


def test_events_are_bounded_per_request(event_hub, event, monkeypatch):
    monkeypatch.setattr(client, "MAX_BATCH_BYTES", 1_000_000)
    AccountingClient(event_hub).record_many({**event, "request_id": f"resp-{index}"} for index in range(101))
    assert [len(call[2]["events"]) for call in event_hub.calls] == [100, 1]


@pytest.fixture
def large_events(event):
    return [{**event, "request_id": f"large-{index}", "source_event_id": f"large-{index}",
             "provenance": "Allocation € 🦊 " * 50, "tokens": {"input": 10, "output": 3}}
            for index in range(100)]


def test_batches_obey_the_encoded_byte_budget(event_hub, large_events):
    AccountingClient(event_hub).record_many(large_events)
    assert max(len(json.dumps(call[2]).encode()) for call in event_hub.calls) <= MAX_BATCH_BYTES


def test_byte_bounded_batches_preserve_every_event_identity(event_hub, large_events):
    receipts = AccountingClient(event_hub).record_many(large_events)
    assert [receipt["event"]["event_id"] for receipt in receipts] == [normalize_event(event)["event_id"] for event in large_events]


def test_byte_bounded_batches_preserve_unicode_provenance(event_hub, large_events):
    receipts = AccountingClient(event_hub).record_many(large_events)
    assert [receipt["event"]["provenance"] for receipt in receipts] == [event["provenance"] for event in large_events]


def test_exact_encoded_budget_accepts_the_boundary(event_hub, event, monkeypatch):
    canonical = normalize_event({**event, "worker_id": "node-a/codex:child", "parent_worker_id": "node-a/codex:parent"})
    monkeypatch.setattr(client, "MAX_BATCH_BYTES", len(json.dumps({"events": [canonical, canonical]}).encode()))
    AccountingClient(event_hub).record_many([event, event])
    assert [len(call[2]["events"]) for call in event_hub.calls] == [2]


def test_one_byte_over_the_budget_splits_a_batch(event_hub, event, monkeypatch):
    canonical = normalize_event({**event, "worker_id": "node-a/codex:child", "parent_worker_id": "node-a/codex:parent"})
    monkeypatch.setattr(client, "MAX_BATCH_BYTES", len(json.dumps({"events": [canonical, canonical]}).encode()) - 1)
    AccountingClient(event_hub).record_many([event, event])
    assert [len(call[2]["events"]) for call in event_hub.calls] == [1, 1]


@pytest.fixture
def oversized_event(event):
    evidence = "🦊" * 1000
    return {**event, "provenance": evidence, "model": "model", "provider": "provider", "wall_seconds": "1",
            "tokens": {key: 1 for key in ("input", "output", "cached_input", "cache_write", "reasoning", "total")}}


def test_single_oversized_event_has_an_actionable_error(event_hub, oversized_event):
    with pytest.raises(AccountingError, match="60000-byte upload budget.*provenance metadata"):
        AccountingClient(event_hub).record_many([oversized_event])


def test_oversized_later_event_prevents_partial_network_import(event_hub, event, oversized_event):
    try:
        AccountingClient(event_hub).record_many([event, oversized_event])
    except AccountingError:
        pass
    assert event_hub.calls == []


def test_empty_import_makes_no_request(event_hub):
    AccountingClient(event_hub).record_many([])
    assert event_hub.calls == []


def test_mismatched_receipt_requires_retry(hub, event):
    hub.answer = {"receipts": [{"status": "recorded", "event": {"event_id": "wrong-id"}}]}
    with pytest.raises(AccountingError, match="receipt"):
        AccountingClient(hub).record_many([event])


def test_missing_response_key_is_actionable(hub):
    hub.answer = {}
    with pytest.raises(AccountingError, match="invalid accounting response"):
        AccountingClient(hub).rates()


def test_cli_default_reads_shared_rates(hub, monkeypatch):
    monkeypatch.setattr(hubclient, "configured", lambda: hub)
    CliRunner().invoke(build_hr_group(), ["list", "--json"])
    assert hub.calls == [("GET", "/v1/accounting/rates", None)]


def test_cli_local_bypasses_shared_rates(hub, monkeypatch):
    monkeypatch.setattr(hubclient, "configured", lambda: hub)
    CliRunner().invoke(build_hr_group(), ["list", "--local", "--json"])
    assert hub.calls == []


def test_server_can_inject_node_identity(event):
    from one_conv.accounting import normalize_event
    assert normalize_event({**event, "node_id": "node-a"})["node_id"] == "node-a"
