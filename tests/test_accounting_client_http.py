"""Real cloud HTTP ingest preserves large Unicode accounting batches and retry identities."""

import socket
import threading
import time

import pytest

from one_conv.accounting.client import MAX_BATCH_BYTES, AccountingClient
from one_conv.control.runtime import application
from one_conv.live.hubclient import HubClient

uvicorn = pytest.importorskip("uvicorn")


@pytest.fixture
def workspace(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    endpoint = f"127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, "https://" + endpoint)
    token = app.state.hub.add_token("node-a")
    sizes = []

    async def measured(scope, receive, send):
        size = 0

        async def read():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            return message

        await app(scope, read, send)
        if scope.get("path") == "/v1/accounting/events":
            sizes.append(size)

    server = uvicorn.Server(uvicorn.Config(measured, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Accounting HTTP fixture did not start")
            time.sleep(0.01)
        client = AccountingClient(HubClient("http://" + endpoint, token, "node-a"))
        yield client, app.state.accounting.ledger, sizes
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.fixture
def events():
    return [{"billing_scope": "large-native-history", "source": "codex", "source_event_id": f"source-{index}",
             "request_id": f"request-{index}", "cost_center_id": "reviewer", "worker_id": "codex:session",
             "timestamp": "2026-10-16T12:00:00Z", "provenance": "Allocation € 🦊 " * 50,
             "tokens": {"input": 100, "output": 20}} for index in range(137)]


@pytest.fixture
def imported(workspace, events):
    client, ledger, sizes = workspace
    return client.record_many(events), client, ledger, sizes


def test_real_http_records_every_large_event(imported, events):
    receipts, _, _, _ = imported
    assert [receipt["status"] for receipt in receipts] == ["recorded"] * len(events)


def test_real_http_enforces_the_serialized_request_budget(imported):
    _, _, _, sizes = imported
    assert max(sizes) <= MAX_BATCH_BYTES


def test_real_http_large_import_uses_multiple_requests(imported):
    _, _, _, sizes = imported
    assert len(sizes) > 1


def test_real_http_preserves_unicode_provenance(imported, events):
    _, _, ledger, _ = imported
    assert [event["provenance"] for event in ledger.list_events(limit=1000)] == [event["provenance"] for event in events]


def test_real_http_retries_deduplicate_all_large_events(imported, events):
    _, client, _, _ = imported
    assert [receipt["status"] for receipt in client.record_many(events)] == ["duplicate"] * len(events)


def test_real_http_retries_keep_event_identities_stable(imported, events):
    first, client, _, _ = imported
    assert [receipt["event"]["event_id"] for receipt in client.record_many(events)] == [receipt["event"]["event_id"] for receipt in first]


def test_real_http_retry_keeps_exactly_one_copy_per_event(imported, events):
    _, client, ledger, _ = imported
    client.record_many(events)
    assert len(ledger.list_events(limit=1000)) == len(events)


def test_real_http_retry_preserves_aggregate_token_count(imported, events):
    _, client, _, _ = imported
    client.record_many(events)
    assert client.summary()["tokens"]["input"]["reported_total"] == 100 * len(events)


def test_real_http_retry_preserves_unknown_cash_coverage(imported, events):
    _, client, _, _ = imported
    client.record_many(events)
    assert client.summary()["cash_cost"] == {"reported": [], "unknown_events": len(events), "complete": False}
