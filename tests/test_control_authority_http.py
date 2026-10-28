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
def workspace(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, origin, worker_config={})
    token = app.state.hub.add_token("cfo")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1))
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
            "spec": {"name": "jack", "topic": "release", "prompt": "Review the change."}}


def test_real_http_peer_cannot_ask_for_a_teammate(workspace, spawn):
    _, client = workspace
    with pytest.raises(HubRefused, match=r"\(403\).*staffing authority"):
        client.request("POST", "/v1/workers", spawn)


def test_real_http_denied_request_posts_nothing(workspace, spawn):
    app, client = workspace
    with pytest.raises(HubRefused):
        client.request("POST", "/v1/workers", spawn)
    assert app.state.hub.history("release") == []


def test_real_http_operator_grant_files_the_teammate_request(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    client.request("POST", "/v1/workers", spawn)
    assert app.state.hub.history("release")[0]["text"].startswith("New teammate @jack: A teammate on authenticated node cfo")


def test_real_http_revoked_node_cannot_ask_for_a_teammate(workspace, spawn):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    app.state.store.revoke_staffing("cfo")
    with pytest.raises(HubRefused, match=r"\(403\)"):
        client.request("POST", "/v1/workers", spawn)


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
    assert client.request("POST", "/v1/workers", spawn)["id"] == spawn["id"]


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
