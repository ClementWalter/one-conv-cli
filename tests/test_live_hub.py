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

from one_conv.live.hub import MAX_BODY, Hub, application


def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
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
    {"id": "short"}, {"text": ""}, {"reply": "maybe"}, {"to": "nowhere"}, {"to": "box/claude:nobody"}])
def test_a_malformed_message_is_refused(two_nodes, bad):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/messages", mac, "POST", {**message("box/claude:bbb"), **bad})[0] in (400, 404)
