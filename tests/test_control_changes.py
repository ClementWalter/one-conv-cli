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
"""The change feed: a read-only long-poll that answers as soon as anything the workspace shows changes."""

import threading
import time

import pytest

from one_conv.control.auth import AuthStore
from one_conv.control.store import ControlStore
from tests import test_control_messages as https_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path)
    store.hub.publish("worker", [{"address": "omp:agent", "name": "builder", "status": "idle"}])
    return store


# --- fingerprint -------------------------------------------------------------------------------------


def test_fingerprint_is_stable_without_changes(store):
    assert store.fingerprint() == store.fingerprint()


def test_fingerprint_changes_on_a_post(store):
    before = store.fingerprint()
    store.send({"id": "a" * 32, "target": "worker/omp:agent", "text": "Hello."})
    assert store.fingerprint() != before


def test_fingerprint_changes_on_a_receipt(store):
    store.send({"id": "a" * 32, "target": "worker/omp:agent", "text": "Hello."})
    before = store.fingerprint()
    store.hub.ack("worker", store.hub.inbox("worker", 0)[0]["delivery"], "accepted")
    assert store.fingerprint() != before


def test_fingerprint_changes_on_a_membership(store):
    before = store.fingerprint()
    store.hub.join("worker", "release", "omp:agent")
    assert store.fingerprint() != before


def test_fingerprint_changes_on_a_rename(store):
    before = store.fingerprint()
    store.hub.publish("worker", [{"address": "omp:agent", "name": "reviewer", "status": "idle"}])
    assert store.fingerprint() != before


def test_reading_state_does_not_change_the_fingerprint(store):
    before = store.fingerprint()
    store.state()
    assert store.fingerprint() == before


# --- over real HTTPS ---------------------------------------------------------------------------------


@pytest.fixture
def feed(real_http_workspace):
    browser = real_http_workspace["browser"]
    return real_http_workspace, browser.get("/api/changes?since=&wait=0").json()["fp"]


def test_first_read_returns_immediately_without_a_change(feed):
    workspace, _ = feed
    assert workspace["browser"].get("/api/changes?since=&wait=25").json()["changed"] is False


def test_a_quiet_feed_answers_at_its_deadline(feed):
    workspace, fingerprint = feed
    started = time.monotonic()
    workspace["browser"].get(f"/api/changes?since={fingerprint}&wait=1")
    assert 0.9 <= time.monotonic() - started < 3


@pytest.fixture
def woken(feed):
    """A post lands half a second into a ten-second wait."""
    workspace, fingerprint = feed
    threading.Timer(0.5, lambda: workspace["browser"].post(
        "/api/messages", json={"id": "a" * 32, "target": "#release", "text": "Kickoff."},
        headers=workspace["headers"])).start()
    started = time.monotonic()
    answer = workspace["browser"].get(f"/api/changes?since={fingerprint}&wait=10", timeout=15).json()
    return answer, time.monotonic() - started


def test_a_change_is_reported(woken):
    assert woken[0]["changed"] is True


def test_a_change_answers_well_before_the_deadline(woken):
    assert woken[1] < 3


def test_a_node_receipt_reaches_the_feed(feed):
    workspace, _ = feed
    workspace["browser"].post("/api/messages", json={"id": "a" * 32, "target": "worker/omp:agent", "text": "Hi."},
                              headers=workspace["headers"]).raise_for_status()
    fingerprint = workspace["browser"].get("/api/changes?since=&wait=0").json()["fp"]
    delivery = workspace["worker"].get("/v1/inbox").json()["messages"][0]["delivery"]
    workspace["worker"].post("/v1/acks", json={"delivery": delivery, "outcome": "accepted"}).raise_for_status()
    assert workspace["browser"].get(f"/api/changes?since={fingerprint}&wait=5").json()["changed"] is True


def test_the_feed_does_not_settle_the_human_mailbox(feed):
    workspace, fingerprint = feed
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "workspace/user",
                                                  "text": "Reply."}).raise_for_status()
    workspace["browser"].get(f"/api/changes?since={fingerprint}&wait=1")
    status = workspace["worker"].get("/v1/messages/" + "f" * 32).json()["recipients"][0]["outcome"]
    assert status == "stored"


def test_the_feed_requires_a_paired_browser(real_http_workspace):
    anonymous = https_tests.httpx.Client(base_url=real_http_workspace["origin"], verify=False, timeout=5)
    assert anonymous.get("/api/changes?since=&wait=0").status_code == 401


@pytest.fixture
def revoked(feed):
    """The device is disconnected half a second into a twenty-second wait."""
    workspace, fingerprint = feed
    threading.Timer(0.5, lambda: AuthStore(workspace["directory"]).revoke(
        workspace["browser"].cookies.get("__Host-oneconv-session"))).start()
    started = time.monotonic()
    response = workspace["browser"].get(f"/api/changes?since={fingerprint}&wait=20", timeout=25)
    return response, time.monotonic() - started


def test_a_device_disconnected_while_waiting_is_refused(revoked):
    assert revoked[0].status_code == 401


def test_a_device_disconnected_while_waiting_is_refused_within_seconds(revoked):
    assert revoked[1] < 3


@pytest.mark.parametrize("query", ["since=XYZ&wait=1", "since=&wait=26", "since=&wait=-1", "since=&wait=nan",
                                   "since=&wait=1e3", "since=" + "a" * 33 + "&wait=1"])
def test_invalid_feed_parameters_are_refused(real_http_workspace, query):
    assert real_http_workspace["browser"].get("/api/changes?" + query).status_code == 400
