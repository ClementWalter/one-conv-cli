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
"""Cloud workspace unread counts: one server-side read mark per conversation, shared by every device."""

import time

import pytest

from one_conv.control.store import ControlStore
from one_conv.live.hub import HubError
from tests import test_control_messages as https_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path)
    store.hub.publish("worker", [{"address": "omp:agent", "name": "builder", "status": "idle"}])
    store.hub.join("worker", "release", "omp:agent")
    return store


def post(store, ident, text="An update.", to="#release"):
    store.hub.send("worker", {"id": ident * 32, "from": "omp:agent", "to": to, "text": text,
                              "reply": "none", "mode": "push"})


def topic_unread(store, topic="release"):
    return next(row["unread"] for row in store.state()["topics"] if row["topic"] == topic)


def thread_unread(store):
    return store.direct()["threads"][0]["unread"]


def test_agent_post_in_a_channel_is_unread(store):
    post(store, "a")
    assert topic_unread(store) == 1


def test_each_agent_post_counts(store):
    post(store, "a")
    post(store, "b")
    assert topic_unread(store) == 2


def test_human_post_in_a_channel_is_not_unread(store):
    store.send({"id": "c" * 32, "target": "#release", "text": "From me."})
    assert topic_unread(store) == 0


def test_history_before_read_tracking_starts_read(store):
    post(store, "a")
    with store.hub._db() as db:
        db.execute("UPDATE read_marks SET at = ? WHERE conversation = '*'", (2**40,))
    assert topic_unread(store) == 0


def test_marking_a_channel_read_clears_it(store):
    post(store, "a")
    last = store.topic("release")["messages"][-1]["at"]
    store.mark_read({"conversation": "#release", "at": last})
    assert topic_unread(store) == 0


def test_a_post_after_the_mark_is_unread_again(store):
    post(store, "a")
    store.mark_read({"conversation": "#release", "at": store.topic("release")["messages"][-1]["at"]})
    post(store, "b")
    assert topic_unread(store) == 1


def test_topic_tagged_direct_message_is_not_a_channel_unread(store):
    store.hub.send("worker", {"id": "d" * 32, "from": "omp:agent", "to": "workspace/user", "topic": "release",
                              "text": "Just for you.", "reply": "none", "mode": "push"})
    assert topic_unread(store) == 0


def test_agent_direct_message_is_unread(store):
    post(store, "a", to="workspace/user")
    assert thread_unread(store) == 1


def test_a_terminal_exchange_mirrored_to_the_workspace_is_not_unread(store):
    store.hub.report("worker", [{"id": "e" * 32, "from": "omp:agent", "to": "user", "topic": None, "mode": "push",
                                 "reply": "none", "text": "Done at the terminal.", "at": time.time() + 60,
                                 "outcome": "accepted"}])
    assert thread_unread(store) == 0


def test_a_direct_message_beside_a_terminal_exchange_is_still_unread(store):
    store.hub.report("worker", [{"id": "e" * 32, "from": "omp:agent", "to": "user", "topic": None, "mode": "push",
                                 "reply": "none", "text": "Done at the terminal.", "at": time.time() + 60,
                                 "outcome": "accepted"}])
    post(store, "a", to="workspace/user")
    assert thread_unread(store) == 1


def test_marking_a_direct_thread_read_clears_it(store):
    post(store, "a", to="workspace/user")
    thread = store.direct()["threads"][0]
    store.mark_read({"conversation": thread["pair"], "at": thread["last"]})
    assert thread_unread(store) == 0


def test_mark_read_returns_the_stored_mark(store):
    store.mark_read({"conversation": "#release", "at": 50.0})
    assert store.mark_read({"conversation": "#release", "at": 10.0}) == {"conversation": "#release", "at": 50.0}


def test_invalid_mark_is_a_client_error(store):
    with pytest.raises(HubError) as error:
        store.mark_read({"conversation": "#release"})
    assert error.value.status == 400


def test_fingerprint_changes_on_a_read_mark(store):
    before = store.fingerprint()
    store.mark_read({"conversation": "#release", "at": 10.0})
    assert store.fingerprint() != before


def test_restarting_the_store_keeps_read_marks(store, tmp_path):
    post(store, "a")
    store.mark_read({"conversation": "#release", "at": store.topic("release")["messages"][-1]["at"]})
    assert topic_unread(ControlStore(tmp_path)) == 0


# --- over real HTTPS ---------------------------------------------------------------------------------


@pytest.fixture
def read_mark():
    return {"conversation": "#release", "at": 10.0}


def test_session_advertises_unread(real_http_workspace):
    assert real_http_workspace["browser"].get("/api/session").json()["capabilities"]["unread"] is True


def test_read_mark_is_saved_over_https(real_http_workspace, read_mark):
    workspace = real_http_workspace
    response = workspace["browser"].post("/api/read", json=read_mark, headers=workspace["headers"])
    assert response.json() == read_mark


@pytest.mark.parametrize("header", ["Origin", "X-One-Conv-Token"])
def test_read_mark_requires_origin_and_token(real_http_workspace, read_mark, header):
    workspace = real_http_workspace
    headers = {key: value for key, value in workspace["headers"].items() if key != header}
    assert workspace["browser"].post("/api/read", json=read_mark, headers=headers).status_code == 403


def test_invalid_read_mark_is_refused_over_https(real_http_workspace):
    workspace = real_http_workspace
    response = workspace["browser"].post("/api/read", json={"conversation": "#Bad", "at": 1},
                                         headers=workspace["headers"])
    assert response.status_code == 400
