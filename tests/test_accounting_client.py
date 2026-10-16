"""Accounting clients retain hub authentication boundaries and reject ambiguous acknowledgments."""

import copy
from urllib.parse import parse_qs, urlsplit

import pytest
from click.testing import CliRunner

from one_conv.accounting.client import AccountingClient, configured_client
from one_conv.accounting.model import AccountingError
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


def test_events_are_bounded_per_request(event_hub, event):
    AccountingClient(event_hub).record_many({**event, "request_id": f"resp-{index}"} for index in range(101))
    assert [len(call[2]["events"]) for call in event_hub.calls] == [100, 1]


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
