"""Loopback HTTP exercises staffing delegation while ordinary peers keep their existing APIs."""

import socket
import threading
import time

import pytest

from one_conv.accounting.client import AccountingClient
from one_conv.control.runtime import application
from one_conv.live.hubclient import HubClient, HubRefused

uvicorn = pytest.importorskip("uvicorn")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from one_conv.control.workers import WorkerManager

    # The actual request dispatcher queues jobs; no supervisor may contact a provider.
    monkeypatch.setattr(WorkerManager, "run_once", lambda self: None)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, origin, worker_config={})
    token = app.state.hub.add_token("cfo")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Staffing HTTP fixture did not start")
            time.sleep(0.01)
        yield app, HubClient(origin, token, "cfo")
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()


@pytest.fixture
def spawn():
    return {"action": "spawn", "id": "a" * 32,
            "spec": {"name": "jack", "harness": "codex", "model": None,
                     "topic": "release", "profile": "default", "prompt": "Review the change."}}


@pytest.mark.parametrize("action", ["spawn", "stop", "recheck", "stop_pool", "resume_pool", "reconcile"])
def test_real_http_peer_cannot_mutate_staffing(workspace, action):
    _, client = workspace
    with pytest.raises(HubRefused, match=r"\(403\).*staffing authority"):
        client.request("POST", "/v1/workers", {"action": action})


def test_real_http_staffing_reads_remain_available(workspace):
    _, client = workspace
    assert client.request("GET", "/v1/workers")["jobs"] == []


def test_real_http_denied_spawn_leaves_no_job(workspace, spawn):
    app, client = workspace
    with pytest.raises(HubRefused):
        client.request("POST", "/v1/workers", spawn)
    assert app.state.workers.manager.list()["jobs"] == []


def test_real_http_operator_grant_queues_one_worker(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    assert client.request("POST", "/v1/workers", spawn)["status"] == "queued"


def test_real_http_authorized_spawn_retains_teammate_attribution(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    job = client.request("POST", "/v1/workers", spawn)
    assert "authenticated node cfo" in job["spec"]["prompt"]


def test_real_http_delegated_node_can_stop_its_queued_job(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    client.request("POST", "/v1/workers", spawn)
    assert client.request("POST", "/v1/workers", {"action": "stop", "id": spawn["id"]})["status"] == "stopped"


def test_real_http_revoke_blocks_even_queued_job_stop(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    client.request("POST", "/v1/workers", spawn)
    app.state.store.revoke_staffing("cfo")
    with pytest.raises(HubRefused, match=r"\(403\)"):
        client.request("POST", "/v1/workers", {"action": "stop", "id": spawn["id"]})


def test_real_http_replacement_token_needs_new_grant(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    client.token = app.state.store.add_node("cfo")
    with pytest.raises(HubRefused, match=r"\(403\)"):
        client.request("POST", "/v1/workers", spawn)


def test_real_http_old_token_remains_revoked_after_regrant(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    app.state.store.add_node("cfo", staffing=True)
    with pytest.raises(HubRefused, match=r"\(401\)"):
        client.request("POST", "/v1/workers", spawn)


def test_real_http_replacement_can_receive_explicit_delegation(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    client.token = app.state.store.add_node("cfo")
    app.state.store.grant_staffing("cfo")
    assert client.request("POST", "/v1/workers", spawn)["status"] == "queued"


def test_real_http_ordinary_peer_can_publish_native_session(workspace):
    _, client = workspace
    assert client.request("PUT", "/v1/sessions", {"sessions": [{"address": "omp:worker", "status": "idle"}]})["published"] == 1


def test_real_http_ordinary_peer_can_message_human(workspace):
    app, client = workspace
    client.request("PUT", "/v1/sessions", {"sessions": [{"address": "omp:worker", "status": "idle"}]})
    client.post({"id": "b" * 32, "from": "omp:worker", "to": "workspace/user", "text": "Ready to review."})
    assert app.state.store.direct()["threads"][0]["messages"][0]["text"] == "Ready to review."


def test_real_http_revoked_staffing_keeps_accounting_ingest(workspace):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    app.state.store.revoke_staffing("cfo")
    receipts = AccountingClient(client).record_many([
        {"billing_scope": "selected-subscription", "source": "codex", "source_event_id": "answer-1",
         "cost_center_id": "jack", "worker_id": "codex:session", "timestamp": "2026-10-16T12:00:00Z",
         "provenance": "native numeric usage", "tokens": {"input": 100, "output": 20}}])
    assert receipts[0]["status"] == "recorded"
