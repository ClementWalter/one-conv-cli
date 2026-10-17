"""Durable human routing is exercised through SQLite, crashes, and a real HTTPS server."""

import concurrent.futures
import shutil
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.requests import Request

from one_conv.control.auth import AuthStore
from one_conv.control.server import application
from one_conv.control.store import ControlStore, validate_message
from one_conv.live import bridge, router
from one_conv.live.hub import LEASE_SECONDS, Hub, HubError, split_address
from one_conv.live.model import envelope


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path)
    store.hub.publish("worker", [{"address": "omp:agent", "name": "builder", "title": "Builder", "status": "idle"}])
    return store


@pytest.fixture
def message():
    return {"id": "a" * 32, "target": "worker/omp:agent", "text": "Please review the result."}


def test_human_return_route_is_published_without_a_bridge(store):
    assert [row["address"] for row in store.hub.directory() if row["node"] == "workspace"] == ["workspace/user"]


def test_injected_hub_cannot_escape_the_personal_store(tmp_path):
    hub = Hub(str(tmp_path / "outside.sqlite3"))
    with pytest.raises(ValueError, match="inside store_dir"):
        ControlStore(tmp_path / "personal", hub)


def test_router_recognizes_the_cloud_human_return_route():
    assert router.remote_address("workspace/user") == ("workspace", "user")


def test_hub_recognizes_the_cloud_human_return_route():
    assert split_address("workspace/user") == ("workspace", "user")


def test_node_extension_identity_comes_from_the_bearer_token(store):
    token = store.hub.add_token("worker")
    request = Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})
    assert store.hub.authenticated_node(request) == "worker"


def test_node_extension_rejects_duplicate_authorization_headers(store):
    token = store.hub.add_token("worker")
    request = Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode()),
                                                     (b"authorization", ("Bearer " + token).encode())]})
    with pytest.raises(HubError):
        store.hub.authenticated_node(request)


def test_node_extension_does_not_accept_browser_cookie_authority(store):
    auth = AuthStore(store.directory)
    cookie = auth.pair(auth.issue_pairing_token())
    request = Request({"type": "http", "headers": [(b"cookie", ("__Host-oneconv-session=" + cookie).encode())]})
    with pytest.raises(HubError):
        store.hub.authenticated_node(request)


def test_cloud_human_envelope_offers_a_direct_return_route():
    assert 'to="workspace/user"' in envelope("Review this.", None, "workspace/user")


def test_cloud_human_envelope_provides_a_terminal_reply_if_the_tool_fails():
    assert "one-conv live send workspace/user --stdin --no-reply --yes" in envelope("Review this.", None, "workspace/user")


def test_a_no_reply_human_message_has_no_terminal_reply_invitation():
    assert "one-conv live send" not in envelope("Finished.", None, "workspace/user", "none")


def test_cloud_human_envelope_never_claims_an_agent_kind():
    assert 'from="workspace/user" kind="human"' in envelope("Review this.", bridge.RemoteSender("workspace/user"))


def test_unpublished_peer_human_retains_the_topic_reply_path():
    assert 'to="mac/user"' not in envelope("Review this.", None, "mac/user", topic="release")


@pytest.mark.parametrize("change", [
    {"from": "worker/omp:agent"}, {"id": "not-hex"}, {"text": "  "}, {"text": "x" * 32001},
    {"target": "unknown"}, {"target": "#../escape"}, {"text": "\ud800"},
])
def test_invalid_browser_message_inputs_are_rejected(message, change):
    with pytest.raises(HubError):
        validate_message({**message, **change})


def test_browser_sender_is_always_human(store, message):
    store.send(message)
    assert store.hub.inbox("worker", 0)[0]["from"] == "workspace/user"


def test_identical_retry_keeps_one_native_delivery(store, message):
    store.send(message)
    store.send(message)
    assert len(store.hub.inbox("worker", 0)) == 1


def test_concurrent_retries_keep_one_native_delivery(store, message):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store.send, [message] * 8))
    assert len(store.hub.inbox("worker", 0)) == 1


def test_changed_payload_cannot_reuse_an_accepted_message_id(store, message):
    store.send(message)
    with pytest.raises(HubError, match="different message"):
        store.send({**message, "text": "Different request."})


def test_restart_preserves_outbound_human_history(store, message, tmp_path):
    store.send(message)
    assert ControlStore(tmp_path).direct()["threads"][0]["messages"][0]["text"] == message["text"]


def test_response_loss_after_commit_can_be_retried_safely(store, message, monkeypatch):
    send = store.hub.send

    def lose_response(node, payload, **kwargs):
        send(node, payload, **kwargs)
        raise OSError("simulated response loss after commit")

    monkeypatch.setattr(store.hub, "send", lose_response)
    with pytest.raises(OSError):
        store.send(message)
    monkeypatch.setattr(store.hub, "send", send)
    store.send(message)
    assert len(store.hub.inbox("worker", 0)) == 1


def test_process_crash_after_claim_does_not_duplicate_delivery(store, message, tmp_path):
    code = ("import os,sys; from pathlib import Path; from one_conv.control.store import ControlStore; "
            "store=ControlStore(Path(sys.argv[1])); "
            "store.send({'id':'a'*32,'target':'worker/omp:agent','text':'Please review the result.'}); os._exit(99)")
    process = subprocess.run([sys.executable, "-O", "-c", code, str(tmp_path)], check=False, timeout=10,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    store.send(message)
    assert (process.returncode, len(store.hub.inbox("worker", 0))) == (99, 1)


def test_human_receipt_distinguishes_storage_from_reading(store, message):
    assert store.send(message)["receipt"]["notice"] == (
        "Stored durably; delivery acceptance does not mean the recipient has read it.")


@pytest.mark.parametrize("outcome", ["refused", "unconfirmed"])
def test_known_delivery_failures_never_replay_a_success_status(store, message, outcome):
    store.send(message)
    delivery = store.hub.inbox("worker", 0)[0]["delivery"]
    store.hub.ack("worker", delivery, outcome)
    assert store.send(message)["status"] == "unconfirmed"


@pytest.fixture
def replied(store, message):
    store.send(message)
    store.hub.send("worker", {"id": "b" * 32, "from": "omp:agent", "to": "workspace/user", "text": "Review completed."})
    return store.direct()


def test_browser_thread_uses_the_local_human_identity(replied):
    assert replied["threads"][0]["pair"] == "user worker/omp:agent"


def test_agent_reply_is_visible_in_the_browser_mailbox(replied):
    assert replied["threads"][0]["messages"][-1]["to"] == "user"


def test_human_mailbox_receipt_is_accepted_without_a_browser_read(store):
    store.hub.send("worker", {"id": "b" * 32, "from": "omp:agent", "to": "workspace/user", "text": "Durable reply."})
    store.refresh_human()
    assert store.hub.status("worker", "b" * 32)["recipients"][0]["outcome"] == "accepted"


def test_restart_keeps_agent_replies_without_a_mac_relay(store, tmp_path):
    store.hub.send("worker", {"id": "b" * 32, "from": "omp:agent", "to": "workspace/user", "text": "Durable reply."})
    assert ControlStore(tmp_path).direct()["threads"][0]["messages"][0]["text"] == "Durable reply."


def test_state_maps_topic_human_members_to_browser_identity(store):
    store.hub.join("workspace", "release", "user")
    assert store.state()["topics"][0]["members"] == ["user"]


def test_topic_posts_remain_visible_without_other_members(store):
    store.hub.join("workspace", "release", "user")
    store.send({"id": "c" * 32, "target": "#release", "text": "Remember this."})
    assert store.topic("release")["messages"][0]["from"] == "user"


def test_human_first_post_creates_an_empty_topic(store):
    store.send({"id": "c" * 32, "target": "#first-post", "text": "Start the discussion."})
    assert store.topic("first-post")["messages"][0]["text"] == "Start the discussion."


def test_human_topic_send_creates_membership(store):
    store.send({"id": "c" * 32, "target": "#first-post", "text": "Start the discussion."})
    assert store.state()["topics"][0]["members"] == ["user"]


def test_human_topic_retry_keeps_one_membership(store):
    message = {"id": "c" * 32, "target": "#first-post", "text": "Start the discussion."}
    store.send(message)
    store.send(message)
    assert store.hub.topics()[0]["members"] == ["workspace/user"]


def test_human_topic_retry_keeps_one_post(store):
    message = {"id": "c" * 32, "target": "#first-post", "text": "Start the discussion."}
    store.send(message)
    store.send(message)
    assert len(store.topic("first-post")["messages"]) == 1


def test_conflicting_message_id_does_not_create_an_unrelated_topic(store):
    store.send({"id": "c" * 32, "target": "#first-post", "text": "Start the discussion."})
    try:
        store.send({"id": "c" * 32, "target": "#unrelated", "text": "Conflicting content."})
    except HubError:
        pass
    assert [topic["topic"] for topic in store.hub.topics()] == ["first-post"]


@pytest.fixture(params=["bridge", "publication"])
def offline_store(store, request):
    with store.hub._db() as db:
        if request.param == "bridge":
            db.execute("UPDATE nodes SET seen=? WHERE node='worker'", (time.time() - LEASE_SECONDS - 1,))
        else:
            db.execute("UPDATE sessions SET updated=? WHERE node='worker'", (time.time() - LEASE_SECONDS - 1,))
    return store


def test_human_new_dm_rejects_offline_route(offline_store, message):
    with pytest.raises(HubError, match="offline"):
        offline_store.send(message)


def test_offline_dm_rejection_stores_no_post(offline_store, message):
    try:
        offline_store.send(message)
    except HubError:
        pass
    with offline_store.hub._db() as db:
        posts = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    assert posts == 0


def test_human_stored_retry_survives_recipient_offline(store, message):
    store.send(message)
    with store.hub._db() as db:
        db.execute("UPDATE nodes SET seen=0 WHERE node='worker'")
    assert store.send(message)["receipt"]["duplicate"] is True


def test_human_stored_retry_survives_disappeared_publication(store, message):
    store.send(message)
    store.hub.publish("worker", [])
    assert store.send(message)["receipt"]["duplicate"] is True


def test_human_offline_retry_still_rejects_changed_payload(store, message):
    store.send(message)
    with store.hub._db() as db:
        db.execute("UPDATE nodes SET seen=0 WHERE node='worker'")
    with pytest.raises(HubError, match="different message"):
        store.send({**message, "text": "Different request."})


def test_direct_messages_expose_adapter_acceptance_without_read_claim(store, message):
    store.send(message)
    delivery = store.hub.inbox("worker", 0)[0]["delivery"]
    store.hub.ack("worker", delivery, "accepted")
    assert store.direct()["threads"][0]["messages"][0]["delivery"]["read_at"] is None


def test_direct_messages_expose_native_acceptance_timestamp(store, message):
    store.send(message)
    delivery = store.hub.inbox("worker", 0)[0]["delivery"]
    store.hub.ack("worker", delivery, "accepted")
    with store.hub._db() as db:
        accepted = db.execute("SELECT outcome_at FROM deliveries WHERE delivery=?", (delivery,)).fetchone()[0]
    assert store.direct()["threads"][0]["messages"][0]["delivery"]["accepted_at"] == accepted


@pytest.fixture(scope="module")
def tls_certificate(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is required for real HTTPS transport checks")
    directory = tmp_path_factory.mktemp("control-tls")
    certificate, key = directory / "certificate.pem", directory / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(certificate), "-days", "1", "-subj", "/CN=127.0.0.1"],
                   check=True, capture_output=True, timeout=15)
    return certificate, key


@pytest.fixture
def real_http_workspace(tmp_path, tls_certificate):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"https://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, origin)
    worker_token = app.state.hub.add_token("worker")
    certificate, key = tls_certificate
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                          ssl_certfile=str(certificate), ssl_keyfile=str(key)))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("HTTPS fixture failed to start")
        time.sleep(0.01)
    browser = httpx.Client(base_url=origin, verify=False, timeout=5)
    worker = httpx.Client(base_url=origin, verify=False, timeout=5,
                          headers={"Authorization": "Bearer " + worker_token})
    try:
        worker.put("/v1/sessions", json={"sessions": [{"address": "omp:agent", "name": "builder", "status": "idle"}]}).raise_for_status()
        token = AuthStore(tmp_path).issue_pairing_token()
        browser.post("/api/login", json={"token": token}, headers={"Origin": origin}).raise_for_status()
        csrf = browser.get("/api/session").json()["csrf_token"]
        yield {"browser": browser, "worker": worker, "headers": {"Origin": origin, "X-One-Conv-Token": csrf},
               "origin": origin, "directory": tmp_path}
    finally:
        browser.close()
        worker.close()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.fixture
def real_http_exchange(real_http_workspace, message):
    browser, worker = (real_http_workspace[key] for key in ("browser", "worker"))
    response = browser.post("/api/messages", json=message, headers=real_http_workspace["headers"])
    response.raise_for_status()
    incoming = worker.get("/v1/inbox").json()["messages"][0]
    worker.post("/v1/acks", json={"delivery": incoming["delivery"], "outcome": "accepted"}).raise_for_status()
    worker.post("/v1/messages", json={"id": "b" * 32, "from": "omp:agent", "to": "workspace/user",
                                      "text": "Reply over real HTTPS."}).raise_for_status()
    direct = browser.get("/api/direct")
    direct.raise_for_status()
    return {"incoming": incoming, "direct": direct.json(), **real_http_workspace}


def test_real_http_send_reaches_the_remote_node(real_http_exchange):
    assert real_http_exchange["incoming"]["from"] == "workspace/user"


def test_real_http_agent_reply_reaches_the_browser(real_http_exchange):
    assert real_http_exchange["direct"]["threads"][0]["messages"][-1]["text"] == "Reply over real HTTPS."


def test_real_http_mailbox_ack_never_claims_human_read(real_http_exchange):
    response = real_http_exchange["worker"].get("/v1/messages/" + "b" * 32)
    assert response.json()["recipients"][0]["outcome"] == "accepted"


def test_real_http_retry_uses_the_same_durable_delivery(real_http_exchange, message):
    workspace = real_http_exchange
    response = workspace["browser"].post("/api/messages", json=message, headers=workspace["headers"])
    assert response.json()["receipt"]["duplicate"] is True


def test_real_http_browser_cannot_select_an_agent_sender(real_http_workspace, message):
    response = real_http_workspace["browser"].post("/api/messages", json={**message, "from": "worker/omp:agent"},
                                                   headers=real_http_workspace["headers"])
    assert response.status_code == 400


def test_real_http_node_token_has_no_browser_session_rights(real_http_workspace):
    assert real_http_workspace["worker"].get("/api/direct").status_code == 401


def test_real_http_state_loaded_route_has_unverified_presence(real_http_workspace):
    real_http_workspace["worker"].put("/v1/sessions", json={"sessions": [{"address": "codex:agent", "status": "loaded"}]}).raise_for_status()
    assert real_http_workspace["browser"].get("/api/state").json()["sessions"][0]["presence"]["state"] == "unverified"


def test_real_http_last_agent_message_is_activity_context(real_http_exchange):
    assert real_http_exchange["browser"].get("/api/state").json()["sessions"][0]["activity"]["summary"] == "Reply over real HTTPS."


def test_real_http_message_acceptance_has_no_read_receipt(real_http_exchange):
    messages = real_http_exchange["browser"].get("/api/direct").json()["threads"][0]["messages"]
    assert messages[0]["delivery"]["read_at"] is None


def test_real_http_history_only_agent_remains_visible(real_http_exchange):
    real_http_exchange["worker"].put("/v1/sessions", json={"sessions": []}).raise_for_status()
    assert real_http_exchange["browser"].get("/api/state").json()["sessions"][0]["history_only"] is True


def test_real_http_stale_agent_rejects_new_human_request(real_http_workspace, message):
    store = ControlStore(real_http_workspace["directory"])
    with store.hub._db() as db:
        db.execute("UPDATE sessions SET updated=0 WHERE node='worker'")
    response = real_http_workspace["browser"].post("/api/messages", json=message, headers=real_http_workspace["headers"])
    assert response.status_code == 409


def test_real_http_stale_agent_preserves_stored_retry(real_http_exchange, message):
    store = ControlStore(real_http_exchange["directory"])
    with store.hub._db() as db:
        db.execute("UPDATE sessions SET updated=0 WHERE node='worker'")
    response = real_http_exchange["browser"].post("/api/messages", json=message, headers=real_http_exchange["headers"])
    assert response.json()["receipt"]["duplicate"] is True
