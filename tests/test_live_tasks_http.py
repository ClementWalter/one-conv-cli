"""Tasks over the hub's real HTTP API: agents act with their node token, and the workspace upgrades old queues."""

import json
import socket
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.control.store import ControlStore  # noqa: E402
from one_conv.live.hub import Hub, application  # noqa: E402


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


def call(url, path, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url + path, data=data, method=method, headers={"Authorization": f"Bearer {token}"})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw.decode(errors="replace")}  # A router 404/405 is plain text.


@pytest.fixture
def workspace(tmp_path):
    """A workspace store with two agents on node mac, served over HTTP with mac's token."""
    store = ControlStore(tmp_path / "store")
    token = store.add_node("mac")
    store.hub.publish("mac", [{"address": "claude:lead", "name": "coordinator"}, {"address": "codex:peer"}])
    url, stop = serve(application(store.hub))
    receipt = store.send({"id": "a" * 32, "target": "#general", "text": "Book the train"})
    yield store, url, token, receipt["receipt"]
    stop()


@pytest.fixture
def key(workspace):
    store, *_ = workspace
    return store.hub.task_list()[0]["key"]


def test_an_agent_reads_its_task_over_http(workspace, key):
    _, url, token, _ = workspace
    status, body = call(url, f"/v1/tasks?key={key}", token)
    assert body["task"]["owner"] == "mac/claude:lead"


def test_the_owner_reports_progress_over_http(workspace, key):
    _, url, token, _ = workspace
    status, body = call(url, "/v1/tasks/update", token, "POST",
                        {"key": key, "actor": "claude:lead", "version": 1, "state": "working", "note": "on it"})
    assert body["task"]["state"] == "working"


def test_an_observer_update_over_http_is_refused_with_the_current_owner(workspace, key):
    _, url, token, _ = workspace
    status, body = call(url, "/v1/tasks/update", token, "POST",
                        {"key": key, "actor": "codex:peer", "version": 1, "state": "done"})
    assert (status, body["task"]["owner"]) == (409, "mac/claude:lead")


def test_an_unpublished_actor_is_refused_over_http(workspace, key):
    _, url, token, _ = workspace
    status, _ = call(url, "/v1/tasks/claim", token, "POST", {"key": key, "actor": "claude:ghost"})
    assert status == 403


def test_the_node_api_has_no_assign_route(workspace, key):
    _, url, token, _ = workspace
    status, _ = call(url, "/v1/tasks/assign", token, "POST", {"key": key, "to": "@coordinator", "version": 1})
    assert status in (404, 405)


def test_the_inbox_hands_the_owner_its_task(workspace, key):
    _, url, token, _ = workspace
    status, body = call(url, "/v1/inbox", token)
    owner = [item for item in body["messages"] if item["to"] == "claude:lead"][0]
    assert (owner["role"], owner["mode"], owner["task"]["key"]) == ("owner", "push", key)


def test_the_inbox_hands_an_observer_context_only(workspace, key):
    _, url, token, _ = workspace
    status, body = call(url, "/v1/inbox", token)
    observer = [item for item in body["messages"] if item["to"] == "codex:peer"][0]
    assert (observer["role"], observer["mode"]) == ("observer", "fyi")


def test_the_workspace_shows_the_owner_on_the_request(workspace, key):
    store, *_ = workspace
    request = [m for m in store.topic("general")["messages"] if m.get("task")][0]
    assert request["task"]["owner"] == "mac/claude:lead"


# --- upgrading a workspace whose hub predates tasks ----------------------------------------------


@pytest.fixture
def upgraded(tmp_path):
    """A hub with a human broadcast still queued from before tasks, then opened by the workspace."""
    directory = tmp_path / "store"
    directory.mkdir(mode=0o700)
    old = Hub(str(directory / "hub.sqlite3"), general="general")
    old.publish("mac", [{"address": "claude:lead", "name": "coordinator"}, {"address": "codex:peer"}])
    old.publish("workspace", [{"address": "user"}])
    old.send("workspace", {"id": "b" * 32, "from": "user", "to": "#general", "text": "Old request",
                           "reply": "expected", "mode": "push"}, join_sender=True)
    store = ControlStore(directory)
    return store


def test_an_upgraded_workspace_delivers_old_queued_requests_as_context(upgraded):
    with sqlite3.connect(upgraded.hub.path) as db:
        modes = {mode for (mode,) in db.execute("SELECT mode FROM deliveries WHERE outcome IS NULL")}
    assert modes == {"fyi"}


def test_an_upgraded_workspace_creates_no_task_for_old_requests(upgraded):
    assert upgraded.hub.task_list() == []


def test_an_upgraded_workspace_keeps_the_old_history(upgraded):
    assert [m["text"] for m in upgraded.topic("general")["messages"]] == ["Old request"]
