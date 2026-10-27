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
"""The live hub over real HTTP: node tokens, publishing, routing, dedup, topics and outcomes."""

import http.client
import json
import socket
import sqlite3
import threading
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.live.hub import MAX_BODY, Hub, HubError, application


def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.02)

    def stop():
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    return f"http://127.0.0.1:{sock.getsockname()[1]}", stop


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve(application(state))
    yield state, url
    stop()


def call(url, path, token, method="GET", body=None):
    """(status, json) of one request; errors are returned, not raised."""
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url + path, data=data, method=method,
                      headers={"Authorization": f"Bearer {token}"} if token else {})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def two_nodes(hub):
    """Nodes `mac` and `box`, each with one published Claude session."""
    state, url = hub
    mac, box = state.add_token("mac"), state.add_token("box")
    call(url, "/v1/sessions", mac, "PUT", {"sessions": [{"address": "claude:aaa", "name": "lead"}]})
    call(url, "/v1/sessions", box, "PUT", {"sessions": [{"address": "claude:bbb", "name": "worker"}]})
    return state, url, mac, box


def message(to, sender="claude:aaa", text="hello", **extra):
    return {"id": uuid.uuid4().hex, "to": to, "from": sender, "text": text, **extra}


def test_a_request_without_a_token_is_rejected(hub):
    _, url = hub
    assert call(url, "/v1/whoami", None)[0] == 401


def test_a_revoked_token_is_rejected(hub):
    state, url = hub
    token = state.add_token("mac")
    state.revoke("mac")
    assert call(url, "/v1/whoami", token)[0] == 401


def test_the_token_decides_the_node(two_nodes):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/whoami", mac)[1] == {"node": "mac"}


def test_the_directory_lists_sessions_by_node(two_nodes):
    _, url, mac, _ = two_nodes
    assert [s["address"] for s in call(url, "/v1/sessions", mac)[1]["sessions"]] == ["box/claude:bbb", "mac/claude:aaa"]


def test_a_name_given_to_two_sessions_is_refused(two_nodes):
    _, url, mac, _ = two_nodes
    sessions = [{"address": "claude:aaa", "name": "lead"}, {"address": "claude:ccc", "name": "lead"}]
    assert call(url, "/v1/sessions", mac, "PUT", {"sessions": sessions})[0] == 409


def test_an_address_listed_twice_is_refused(two_nodes):
    _, url, mac, _ = two_nodes
    sessions = [{"address": "claude:aaa", "status": "idle"}, {"address": "claude:aaa", "status": "busy"}]
    assert call(url, "/v1/sessions", mac, "PUT", {"sessions": sessions})[0] == 400


def test_a_published_git_identity_is_kept(tmp_path):
    hub = Hub(str(tmp_path / "hub.db"))
    hub.publish("mac", [], spawn="claude", priority=10, git_user={"name": "Ada", "email": "ada@example.test"})
    assert hub.git_identity(exclude="box") == {"name": "Ada", "email": "ada@example.test"}


def test_a_publication_without_identity_keeps_the_last_one(tmp_path):
    hub = Hub(str(tmp_path / "hub.db"))
    hub.publish("mac", [], git_user={"name": "Ada", "email": "ada@example.test"})
    hub.publish("mac", [])
    assert hub.git_identity(exclude="box") == {"name": "Ada", "email": "ada@example.test"}


def test_a_bad_git_identity_is_refused(two_nodes):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/sessions", mac, "PUT", {"sessions": [], "git_user": {"name": "Ada"}})[0] == 400


def test_the_directory_carries_a_published_product(two_nodes):
    _, url, mac, _ = two_nodes
    call(url, "/v1/sessions", mac, "PUT", {"sessions": [{"address": "cursor:k1", "product": "cursor-cli"}]})
    sessions = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert next(s for s in sessions if s["address"] == "mac/cursor:k1")["product"] == "cursor-cli"


def test_a_session_without_a_product_lists_an_empty_one(two_nodes):
    _, url, mac, _ = two_nodes
    sessions = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert next(s for s in sessions if s["address"] == "mac/claude:aaa")["product"] == ""


def test_an_unknown_product_is_refused(two_nodes):
    _, url, mac, _ = two_nodes
    sessions = [{"address": "cursor:k1", "product": "cursor-web"}]
    assert call(url, "/v1/sessions", mac, "PUT", {"sessions": sessions})[0] == 400


def test_a_hub_from_before_products_gains_the_column(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (node TEXT NOT NULL, address TEXT NOT NULL, name TEXT, title TEXT, cwd TEXT,"
                   " status TEXT, updated REAL NOT NULL, PRIMARY KEY (node, address))")
        db.execute("INSERT INTO sessions VALUES ('mac', 'claude:aaa', NULL, '', '', '', 1.0)")
    assert Hub(str(path)).directory()[0]["product"] == ""


def test_two_unnamed_sessions_publish(two_nodes):
    _, url, mac, _ = two_nodes
    sessions = [{"address": "claude:aaa"}, {"address": "claude:ccc"}]
    assert call(url, "/v1/sessions", mac, "PUT", {"sessions": sessions})[1] == {"published": 2}


def test_a_publishing_node_is_listed_online(two_nodes):
    _, url, mac, _ = two_nodes
    assert all(s["online"] for s in call(url, "/v1/sessions", mac)[1]["sessions"])


def test_a_node_that_stopped_renewing_is_listed_offline(two_nodes):
    state, url, mac, _ = two_nodes
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE nodes SET seen = 0 WHERE node = 'box'")
    rows = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert {s["node"]: s["online"] for s in rows} == {"box": False, "mac": True}


def test_inbox_polling_cannot_keep_an_old_session_publication_online(two_nodes):
    state, url, mac, box = two_nodes
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE sessions SET updated = 0 WHERE node = 'box'")
    call(url, "/v1/inbox?wait=0", box)
    rows = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert next(row for row in rows if row["node"] == "box")["online"] is False


def test_directory_keeps_session_publication_time_separate_from_bridge_polling(two_nodes):
    state, url, mac, box = two_nodes
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE sessions SET updated = 123 WHERE node = 'box'")
    call(url, "/v1/inbox?wait=0", box)
    rows = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert next(row for row in rows if row["node"] == "box")["published_at"] == 123


@pytest.fixture(params=["nodes", "sessions"])
def stale_recipient(two_nodes, request):
    state, *_ = two_nodes
    column = "seen" if request.param == "nodes" else "updated"
    with sqlite3.connect(state.path) as db:
        db.execute(f"UPDATE {request.param} SET {column} = 0 WHERE node = 'box'")
    return two_nodes


def test_human_online_gate_refuses_a_stale_recipient(stale_recipient):
    state, *_ = stale_recipient
    with pytest.raises(HubError, match="Recipient is offline"):
        state.send("mac", message("box/claude:bbb"), require_online=True)


def test_machine_queueing_remains_available_for_a_stale_recipient(stale_recipient):
    state, *_ = stale_recipient
    assert len(state.send("mac", message("box/claude:bbb"))["recipients"]) == 1


def test_online_gate_keeps_the_original_receipt_after_the_recipient_disconnects(two_nodes):
    state, *_ = two_nodes
    sent = message("box/claude:bbb")
    state.send("mac", sent, require_online=True)
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE sessions SET updated = 0 WHERE node = 'box'")
    assert state.send("mac", sent, require_online=True)["duplicate"] is True


def test_online_gate_does_not_accept_a_changed_retry_after_disconnect(two_nodes):
    state, *_ = two_nodes
    sent = message("box/claude:bbb")
    state.send("mac", sent, require_online=True)
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE sessions SET updated = 0 WHERE node = 'box'")
    with pytest.raises(HubError, match="different message"):
        state.send("mac", {**sent, "text": "Different request"}, require_online=True)


def test_a_name_used_on_another_node_is_refused(two_nodes):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/sessions", mac, "PUT", {"sessions": [{"address": "claude:aaa", "name": "worker"}]})[0] == 409


def test_a_message_reaches_the_target_nodes_inbox(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("box/claude:bbb", text="run the tests"))
    assert [m["text"] for m in call(url, "/v1/inbox?wait=0", box)[1]["messages"]] == ["run the tests"]


def test_the_inbox_names_the_sender_with_its_node(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("@worker"))
    assert call(url, "/v1/inbox?wait=0", box)[1]["messages"][0]["from"] == "mac/claude:aaa"


def test_a_sender_cannot_claim_another_nodes_session(two_nodes):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/messages", mac, "POST", message("box/claude:bbb", sender="claude:bbb"))[0] == 403


def test_a_retried_message_id_is_stored_once(two_nodes):
    _, url, mac, box = two_nodes
    sent = message("box/claude:bbb")
    call(url, "/v1/messages", mac, "POST", sent)
    call(url, "/v1/messages", mac, "POST", sent)
    assert len(call(url, "/v1/inbox?wait=0", box)[1]["messages"]) == 1


def test_a_retried_message_id_reports_a_duplicate(two_nodes):
    _, url, mac, _ = two_nodes
    sent = message("box/claude:bbb")
    call(url, "/v1/messages", mac, "POST", sent)
    assert call(url, "/v1/messages", mac, "POST", sent)[1]["duplicate"] is True


@pytest.mark.parametrize("sent, mode", [({}, "push"), ({"mode": "fyi"}, "fyi")])
def test_the_inbox_carries_the_delivery_mode(two_nodes, sent, mode):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("box/claude:bbb", reply="none", **sent))
    assert call(url, "/v1/inbox?wait=0", box)[1]["messages"][0]["mode"] == mode


def test_an_fyi_and_a_no_reply_push_under_one_id_conflict(two_nodes):
    _, url, mac, _ = two_nodes
    sent = message("box/claude:bbb", reply="none")
    call(url, "/v1/messages", mac, "POST", sent)
    assert call(url, "/v1/messages", mac, "POST", {**sent, "mode": "fyi"})[0] == 409


def test_a_lost_response_is_answered_by_the_original_receipt(two_nodes):
    _, url, mac, _ = two_nodes
    sent = message("box/claude:bbb")
    first = call(url, "/v1/messages", mac, "POST", sent)[1]
    retry = call(url, "/v1/messages", mac, "POST", sent)[1]
    assert retry["recipients"] == first["recipients"]


def test_a_reused_id_with_a_different_payload_is_a_conflict(two_nodes):
    _, url, mac, _ = two_nodes
    sent = message("box/claude:bbb")
    call(url, "/v1/messages", mac, "POST", sent)
    assert call(url, "/v1/messages", mac, "POST", {**sent, "text": "something else"})[0] == 409


@pytest.fixture
def same_id_both_ways(two_nodes):
    """mac and box each send a message under the same id."""
    _, url, mac, box = two_nodes
    sent = message("box/claude:bbb", text="from mac")
    call(url, "/v1/messages", mac, "POST", sent)
    call(url, "/v1/messages", box, "POST", {**sent, "from": "claude:bbb", "to": "mac/claude:aaa", "text": "from box"})
    return url, mac, box, sent["id"]


def test_another_nodes_message_under_the_same_id_is_delivered(same_id_both_ways):
    url, mac, _, _ = same_id_both_ways
    assert [m["text"] for m in call(url, "/v1/inbox?wait=0", mac)[1]["messages"]] == ["from box"]


def test_another_nodes_message_under_the_same_id_keeps_its_own_status(same_id_both_ways):
    url, _, box, msg_id = same_id_both_ways
    assert [r["to"] for r in call(url, f"/v1/messages/{msg_id}", box)[1]["recipients"]] == ["mac/claude:aaa"]


def delivery_of(url, token):
    return call(url, "/v1/inbox?wait=0", token)[1]["messages"][0]["delivery"]


def test_an_acknowledged_delivery_leaves_the_inbox(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("box/claude:bbb"))
    call(url, "/v1/acks", box, "POST", {"delivery": delivery_of(url, box), "outcome": "accepted"})
    assert call(url, "/v1/inbox?wait=0", box)[1]["messages"] == []


def test_an_unacknowledged_delivery_is_handed_out_again(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("box/claude:bbb"))
    first = delivery_of(url, box)
    assert delivery_of(url, box) == first


def test_only_the_receiving_node_reports_an_outcome(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("box/claude:bbb"))
    assert call(url, "/v1/acks", mac, "POST", {"delivery": delivery_of(url, box), "outcome": "accepted"})[0] == 404


def test_the_first_reported_outcome_stands(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("box/claude:bbb"))
    delivery = delivery_of(url, box)
    call(url, "/v1/acks", box, "POST", {"delivery": delivery, "outcome": "unconfirmed"})
    assert call(url, "/v1/acks", box, "POST", {"delivery": delivery, "outcome": "accepted"})[1]["outcome"] == "unconfirmed"


def test_the_sender_reads_the_reported_outcome(two_nodes):
    _, url, mac, box = two_nodes
    sent = message("box/claude:bbb")
    call(url, "/v1/messages", mac, "POST", sent)
    call(url, "/v1/acks", box, "POST", {"delivery": delivery_of(url, box), "outcome": "refused"})
    assert call(url, f"/v1/messages/{sent['id']}", mac)[1]["recipients"][0]["outcome"] == "refused"


def test_another_node_cannot_read_a_messages_status(two_nodes):
    _, url, mac, box = two_nodes
    sent = message("box/claude:bbb")
    call(url, "/v1/messages", mac, "POST", sent)
    assert call(url, f"/v1/messages/{sent['id']}", box)[0] == 404


def test_a_long_poll_returns_a_message_sent_while_waiting(two_nodes):
    _, url, mac, box = two_nodes
    threading.Timer(0.5, lambda: call(url, "/v1/messages", mac, "POST", message("box/claude:bbb"))).start()
    assert len(call(url, "/v1/inbox?wait=5", box)[1]["messages"]) == 1


@pytest.fixture
def topic(two_nodes):
    """#release with mac's lead and box's worker as members."""
    state, url, mac, box = two_nodes
    call(url, "/v1/topics/release/members/claude:aaa", mac, "PUT")
    call(url, "/v1/topics/release/members/claude:bbb", box, "PUT")
    return state, url, mac, box


def test_a_topic_post_reaches_the_other_members(topic):
    _, url, mac, box = topic
    call(url, "/v1/messages", mac, "POST", message("#release", text="tag it"))
    assert [m["topic"] for m in call(url, "/v1/inbox?wait=0", box)[1]["messages"]] == ["release"]


def test_a_topic_post_reaches_every_member_under_one_id(topic):
    state, url, mac, box = topic
    third = state.add_token("third")
    call(url, "/v1/sessions", third, "PUT", {"sessions": [{"address": "codex:ccc"}]})
    call(url, "/v1/topics/release/members/codex:ccc", third, "PUT")
    call(url, "/v1/messages", mac, "POST", message("#release"))
    inboxes = [call(url, "/v1/inbox?wait=0", token)[1]["messages"] for token in (box, third)]
    assert [len(messages) for messages in inboxes] == [1, 1]


def test_a_topic_post_with_no_one_else_there_is_kept_for_later_members(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/topics/release/members/claude:bbb", box, "PUT")
    call(url, "/v1/messages", box, "POST", message("#release", sender="claude:bbb", text="early"))
    call(url, "/v1/topics/release/members/claude:aaa", mac, "PUT")
    assert [m["text"] for m in call(url, "/v1/topics/release/messages", mac)[1]["messages"]] == ["early"]


def test_a_topic_post_skips_its_sender(topic):
    _, url, mac, _ = topic
    call(url, "/v1/messages", mac, "POST", message("#release"))
    assert call(url, "/v1/inbox?wait=0", mac)[1]["messages"] == []


def test_a_node_cannot_add_another_nodes_session_to_a_topic(topic):
    _, url, mac, _ = topic
    assert call(url, "/v1/topics/release/members/claude:bbb", mac, "PUT")[0] == 403


def test_topics_list_their_members(topic):
    _, url, mac, _ = topic
    assert call(url, "/v1/topics", mac)[1]["topics"][0]["members"] == ["mac/claude:aaa", "box/claude:bbb"]


def test_topic_history_holds_only_that_topic(topic):
    _, url, mac, box = topic
    call(url, "/v1/topics/other/members/claude:bbb", box, "PUT")
    call(url, "/v1/messages", mac, "POST", message("#release", text="in release"))
    call(url, "/v1/messages", mac, "POST", message("#other", text="in other"))
    assert [m["text"] for m in call(url, "/v1/topics/release/messages", box)[1]["messages"]] == ["in release"]


def test_topic_history_lists_a_post_once_for_all_its_recipients(topic):
    state, url, mac, box = topic
    third = state.add_token("third")
    call(url, "/v1/sessions", third, "PUT", {"sessions": [{"address": "codex:ccc"}]})
    call(url, "/v1/topics/release/members/codex:ccc", third, "PUT")
    call(url, "/v1/messages", mac, "POST", message("#release"))
    assert len(call(url, "/v1/topics/release/messages", box)[1]["messages"]) == 1


def test_topic_history_after_a_cursor_returns_the_later_ones(topic):
    _, url, mac, box = topic
    call(url, "/v1/messages", mac, "POST", message("#release", text="first"))
    call(url, "/v1/messages", mac, "POST", message("#release", text="second"))
    cursor = call(url, "/v1/topics/release/messages?limit=1", box)[1]["messages"][0]["cursor"]
    page = call(url, f"/v1/topics/release/messages?after={cursor}", box)[1]["messages"]
    assert [m["text"] for m in page] == ["second"]


def test_topic_history_pages_through_two_nodes_sharing_an_id(topic):
    _, url, mac, box = topic
    shared = uuid.uuid4().hex
    call(url, "/v1/messages", mac, "POST", {**message("#release", text="mac"), "id": shared})
    call(url, "/v1/messages", box, "POST", {**message("#release", sender="claude:bbb", text="box"), "id": shared})
    call(url, "/v1/messages", mac, "POST", message("#release", text="third"))
    seen, cursor = [], "0"
    for _ in range(4):
        page = call(url, f"/v1/topics/release/messages?limit=1&after={cursor}", box)[1]["messages"]
        if not page:
            break
        seen.append(page[0]["text"])
        cursor = page[0]["cursor"]
    assert seen == ["mac", "box", "third"]


def raw_post(url, token, body, headers):
    """Status of a POST sent with http.client, so the body can be streamed in chunks."""
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        connection.request("POST", "/v1/messages", body=body, headers={"Authorization": f"Bearer {token}", **headers})
        return connection.getresponse().status
    finally:
        connection.close()


def test_a_body_declared_too_large_is_refused(two_nodes):
    _, url, mac, _ = two_nodes
    assert raw_post(url, mac, b"{}", {"Content-Length": str(MAX_BODY + 1)}) == 413


def chunked_status_line(url, token, total: int) -> bytes:
    """Stream `total` bytes as a chunked POST and return the status line the hub answers with.

    The hub may answer and close before the body is complete, so a broken pipe ends the upload.
    """
    parts = urlsplit(url)
    with socket.create_connection((parts.hostname, parts.port), timeout=10) as sock:
        sock.sendall(f"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
                     "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n".encode())
        chunk = b"10000\r\n" + b"x" * 0x10000 + b"\r\n"
        try:
            for _ in range(total // 0x10000 + 1):
                sock.sendall(chunk)
            sock.sendall(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        return sock.makefile("rb").readline()


def test_a_chunked_body_growing_too_large_is_refused(two_nodes):
    _, url, mac, _ = two_nodes
    assert chunked_status_line(url, mac, MAX_BODY + 0x10000).split()[1] == b"413"


@pytest.mark.parametrize("bad", [
    {"id": "short"}, {"text": ""}, {"reply": "maybe"}, {"mode": "wake"}, {"to": "nowhere"}, {"to": "box/claude:nobody"}])
def test_a_malformed_message_is_refused(two_nodes, bad):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/messages", mac, "POST", {**message("box/claude:bbb"), **bad})[0] in (400, 404)


def test_a_cursor_past_any_sqlite_integer_returns_an_empty_page(topic):
    _, url, mac, box = topic
    call(url, "/v1/messages", mac, "POST", message("#release"))
    assert call(url, "/v1/topics/release/messages?after=9223372036854775808", box) == (200, {"messages": []})
