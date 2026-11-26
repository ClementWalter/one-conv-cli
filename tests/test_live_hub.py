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
"""The live relay over real HTTP: node tokens, publishing, routing, dedup and outcomes."""

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
from relay_worker import BACKENDS, local_database


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


@pytest.fixture(params=BACKENDS)
def hub(request, tmp_path):
    """(relay state, URL): the Python relay, or the Worker relay when ONE_CONV_TEST_RELAY_WORKER=1."""
    if request.param == "worker":
        state = request.getfixturevalue("relay_worker").hub()
        yield state, state.url
        return
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


@pytest.fixture
def database_with_other_tables(tmp_path):
    """A relay database that also holds tables and columns this relay never reads."""
    path = tmp_path / "shared.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE members (topic TEXT NOT NULL, node TEXT NOT NULL, address TEXT NOT NULL)")
        db.execute("CREATE TABLE posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,"
                   " digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, topic TEXT, reply TEXT NOT NULL,"
                   " mode TEXT NOT NULL DEFAULT 'push', text TEXT NOT NULL, created REAL NOT NULL, kind TEXT,"
                   " attachments TEXT, UNIQUE (from_node, id))")
    return path


def test_a_database_with_other_tables_still_relays(database_with_other_tables):
    hub = Hub(str(database_with_other_tables))
    hub.publish("mac", [{"address": "claude:aaa"}])
    hub.publish("box", [{"address": "claude:bbb"}])
    hub.send("mac", message("box/claude:bbb", text="still routed"))
    assert [m["text"] for m in hub.inbox("box", 0)] == ["still routed"]


def test_two_unnamed_sessions_publish(two_nodes):
    _, url, mac, _ = two_nodes
    sessions = [{"address": "claude:aaa"}, {"address": "claude:ccc"}]
    assert call(url, "/v1/sessions", mac, "PUT", {"sessions": sessions})[1] == {"published": 2}


def test_a_publishing_node_is_listed_online(two_nodes):
    _, url, mac, _ = two_nodes
    assert all(s["online"] for s in call(url, "/v1/sessions", mac)[1]["sessions"])


def test_a_node_that_stopped_renewing_is_listed_offline(two_nodes):
    state, url, mac, _ = two_nodes
    with sqlite3.connect(local_database(state)) as db:
        db.execute("UPDATE nodes SET seen = 0 WHERE node = 'box'")
    rows = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert {s["node"]: s["online"] for s in rows} == {"box": False, "mac": True}


def test_inbox_polling_cannot_keep_an_old_session_publication_online(two_nodes):
    state, url, mac, box = two_nodes
    with sqlite3.connect(local_database(state)) as db:
        db.execute("UPDATE sessions SET updated = 0 WHERE node = 'box'")
    call(url, "/v1/inbox?wait=0", box)
    rows = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert next(row for row in rows if row["node"] == "box")["online"] is False


def test_directory_keeps_session_publication_time_separate_from_bridge_polling(two_nodes):
    state, url, mac, box = two_nodes
    with sqlite3.connect(local_database(state)) as db:
        db.execute("UPDATE sessions SET updated = 123 WHERE node = 'box'")
    call(url, "/v1/inbox?wait=0", box)
    rows = call(url, "/v1/sessions", mac)[1]["sessions"]
    assert next(row for row in rows if row["node"] == "box")["published_at"] == 123


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
    {"id": "short"}, {"text": ""}, {"reply": "maybe"}, {"mode": "wake"}, {"to": "nowhere"}, {"to": "box/claude:nobody"},
    {"to": "#release"}, {"to": "@nobody"}])
def test_a_malformed_message_is_refused(two_nodes, bad):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/messages", mac, "POST", {**message("box/claude:bbb"), **bad})[0] in (400, 404)


# --- messages to the user ------------------------------------------------------------------------

def test_a_message_to_user_is_stored_for_the_person(two_nodes):
    _, url, mac, _ = two_nodes
    sent = message("user")
    assert call(url, "/v1/messages", mac, "POST", sent)[1] == {
        "id": sent["id"], "duplicate": False, "recipients": [{"to": "user", "status": "stored"}]}


def test_any_node_reads_a_note_another_node_filed(two_nodes):
    _, url, mac, box = two_nodes
    call(url, "/v1/messages", mac, "POST", message("user", text="PR is green", name="@lead"))
    notes = call(url, "/v1/user", box)[1]["messages"]
    assert [(n["from"], n["name"], n["text"]) for n in notes] == [("mac/claude:aaa", "@lead", "PR is green")]


def test_a_retried_note_is_stored_once(two_nodes):
    _, url, mac, _ = two_nodes
    sent = message("user")
    call(url, "/v1/messages", mac, "POST", sent)
    call(url, "/v1/messages", mac, "POST", sent)
    assert len(call(url, "/v1/user", mac)[1]["messages"]) == 1


def test_a_note_wakes_no_bridge(two_nodes):
    _, url, mac, _ = two_nodes
    call(url, "/v1/messages", mac, "POST", message("user"))
    assert call(url, "/v1/inbox", mac)[1] == {"messages": []}


def test_after_skips_notes_already_read(two_nodes):
    _, url, mac, _ = two_nodes
    call(url, "/v1/messages", mac, "POST", message("user", text="one"))
    call(url, "/v1/messages", mac, "POST", message("user", text="two"))
    assert [n["text"] for n in call(url, "/v1/user?after=1", mac)[1]["messages"]] == ["two"]


def test_a_tag_keeps_only_notes_carrying_it(two_nodes):
    _, url, mac, _ = two_nodes
    call(url, "/v1/messages", mac, "POST", message("user", text="deploy done", tags=["deploy"]))
    call(url, "/v1/messages", mac, "POST", message("user", text="idea", tags=["ideas"]))
    assert [n["text"] for n in call(url, "/v1/user?tag=deploy", mac)[1]["messages"]] == ["deploy done"]


def test_last_is_the_newest_note_even_when_the_tag_skips_it(two_nodes):
    _, url, mac, _ = two_nodes
    call(url, "/v1/messages", mac, "POST", message("user", tags=["deploy"]))
    call(url, "/v1/messages", mac, "POST", message("user", tags=["ideas"]))
    assert call(url, "/v1/user?tag=deploy", mac)[1]["last"] == 2


@pytest.mark.parametrize("extra,status", [
    ({"tags": ["Deploy"]}, 400),
    ({"tags": list("abcdefghi")}, 400),
    ({"name": 3}, 400),
    ({"attachments": ["0" * 32]}, 400),
    ({"from": "claude:bbb"}, 403),
], ids=["upper-case tag", "too many tags", "non-string name", "attachment", "another node's session"])
def test_a_malformed_note_is_refused(two_nodes, extra, status):
    _, url, mac, _ = two_nodes
    assert call(url, "/v1/messages", mac, "POST", message("user", **extra))[0] == status


# --- invites: a connected node lets a new machine join without copying a token ------------------

@pytest.fixture
def inviter(hub):
    """(state, url, token of the connected node `mac`)."""
    state, url = hub
    return state, url, state.add_token("mac")


def invite(url, token, node="studio", **extra):
    return call(url, "/v1/invites", token, "POST", {"node": node, **extra})


def join(url, code):
    return call(url, "/v1/join", None, "POST", {"code": code})


def test_an_invite_needs_a_node_token(hub):
    _, url = hub
    assert invite(url, None)[0] == 401


def test_an_invite_is_bound_to_its_node(inviter):
    _, url, mac = inviter
    assert invite(url, mac)[1]["node"] == "studio"


def test_a_redeemed_invite_gives_the_node_its_token(inviter):
    _, url, mac = inviter
    token = join(url, invite(url, mac)[1]["code"])[1]["token"]
    assert call(url, "/v1/whoami", token)[1] == {"node": "studio"}


def test_an_invite_works_once(inviter):
    _, url, mac = inviter
    code = invite(url, mac)[1]["code"]
    join(url, code)
    assert join(url, code)[0] == 410


def test_an_unknown_code_is_refused(inviter):
    _, url, _ = inviter
    assert join(url, "not-a-code-anyone-was-given")[0] == 404


def test_a_join_without_a_code_is_refused(inviter):
    _, url, _ = inviter
    assert call(url, "/v1/join", None, "POST", {})[0] == 400


def test_an_invite_for_a_node_with_a_token_is_refused(inviter):
    _, url, mac = inviter
    assert invite(url, mac, node="mac")[0] == 409


def test_an_invite_with_replace_takes_over_the_node(inviter):
    _, url, mac = inviter
    join(url, invite(url, mac, node="mac", replace=True)[1]["code"])
    assert call(url, "/v1/whoami", mac)[0] == 401


def test_an_invite_without_replace_is_refused_once_the_node_got_a_token(inviter):
    state, url, mac = inviter
    code = invite(url, mac)[1]["code"]
    state.add_token("studio")
    assert join(url, code)[0] == 409


@pytest.mark.parametrize("ttl", [59, 86401, "15m", True, 900.5])
def test_an_invite_ttl_outside_a_minute_to_a_day_is_refused(inviter, ttl):
    _, url, mac = inviter
    assert invite(url, mac, ttl=ttl)[0] == 400


def test_an_invite_for_a_malformed_node_is_refused(inviter):
    _, url, mac = inviter
    assert invite(url, mac, node="Studio Mac")[0] == 400


def test_an_invite_expires_after_its_ttl(inviter):
    state, url, mac = inviter
    code = invite(url, mac)[1]["code"]
    with sqlite3.connect(local_database(state)) as db:
        db.execute("UPDATE invites SET expires = 0")
    assert join(url, code)[0] == 410


def test_the_relay_keeps_only_a_digest_of_the_code(inviter):
    state, url, mac = inviter
    code = invite(url, mac)[1]["code"]
    with sqlite3.connect(local_database(state)) as db:
        rows = db.execute("SELECT * FROM invites").fetchall()
    assert code not in json.dumps(rows)
