"""Stopping an agent from the web app: the controller orders the agent's bridge, which ends its tmux session.

The order is a system post to `stop:<address>` on the agent's machine. Only that bridge acts on
it, only for a session its spawner started, and it never shows as a message in any conversation.
"""

import threading
import time
import uuid

import pytest
from starlette.testclient import TestClient

from one_conv.control import store as store_module
from one_conv.control.server import application
from one_conv.control.store import ControlStore
from one_conv.live import bridge, spawn
from one_conv.live.hub import HubError

ORIGIN = "https://workspace.example.test"
AGENT = "mac/claude:aaa"


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path / "control")
    store.hub.publish("mac", [{"address": "claude:aaa", "title": "Spawned", "status": "idle"}])
    return store


@pytest.fixture
def ordered(store):
    """The human asked to stop the agent; the answer comes back without waiting for the bridge."""
    return store.stop_session({"address": AGENT}, wait=0)


def stop_orders(store):
    return [item for item in store.hub.inbox("mac", 0) if item["to"].startswith("stop:")]


def answer_soon(store, outcome):
    """The Mac's bridge takes the order from its inbox and reports `outcome`, as `live serve` does."""
    def run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            items = stop_orders(store)
            if items:
                store.hub.ack("mac", items[0]["delivery"], outcome)
                return
            time.sleep(0.05)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# --- the hub's order -------------------------------------------------------------------------------


def test_the_order_goes_to_the_agents_machine(store, ordered):
    assert [item["to"] for item in stop_orders(store)] == ["stop:claude:aaa"]


def test_the_order_is_marked_as_the_hubs_own(store, ordered):
    assert stop_orders(store)[0]["system"] is True


def test_the_answer_is_pending_while_the_bridge_has_not_reported(ordered):
    assert ordered["status"] == "pending"


def test_an_offline_agent_cannot_be_stopped(store, monkeypatch):
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
    with pytest.raises(HubError, match="offline"):
        store.stop_session({"address": AGENT}, wait=0)


def test_the_human_cannot_be_stopped(store):
    with pytest.raises(HubError, match="only an agent"):
        store.stop_session({"address": "workspace/user"}, wait=0)


def test_an_unknown_payload_is_refused(store):
    with pytest.raises(HubError, match="address"):
        store.stop_session({"address": AGENT, "force": True}, wait=0)


def test_the_order_is_not_a_direct_conversation(store, ordered):
    assert store.direct()["threads"] == []


def test_the_order_adds_no_contact_to_the_team(store, ordered):
    assert [row["address"] for row in store.state()["sessions"]] == [AGENT]


# --- waiting for the bridge's answer ---------------------------------------------------------------


def test_an_accepted_order_reads_as_stopped(store):
    answer_soon(store, "accepted")
    assert store.stop_session({"address": AGENT}, wait=5)["status"] == "stopped"


def test_a_refused_order_reads_as_refused(store):
    answer_soon(store, "refused")
    assert store.stop_session({"address": AGENT}, wait=5)["status"] == "refused"


# --- the bridge ------------------------------------------------------------------------------------


@pytest.fixture
def stopped(monkeypatch):
    """spawn.stop stub: records the address it was asked to end, as a tmux session that ended."""
    calls = []
    monkeypatch.setattr(spawn, "stop", lambda target: calls.append(target) or {"tmux": "oneconv-claude-1", "address": target})
    return calls


def order(**extra):
    return {"to": "stop:claude:aaa", "mode": "push", "text": "Stop claude:aaa", "delivery": uuid.uuid4().hex,
            "system": True, **extra}


def test_the_bridge_ends_the_spawned_session(stopped):
    bridge.deliver_inbound(order())
    assert stopped == ["claude:aaa"]


def test_the_bridge_reports_the_stop_accepted(stopped):
    assert bridge.deliver_inbound(order())[0] == "accepted"


def test_the_bridge_republishes_after_stopping(stopped):
    republished = []
    bridge.deliver_inbound(order(), on_started=lambda: republished.append(True))
    assert republished == [True]


def test_a_stop_not_issued_by_the_hub_ends_nothing(stopped):
    bridge.deliver_inbound(order(system=False))
    assert stopped == []


def test_a_stop_not_issued_by_the_hub_is_refused(stopped):
    assert bridge.deliver_inbound(order(system=False))[0] == "refused"


def test_a_session_the_spawner_did_not_start_is_refused(monkeypatch):
    monkeypatch.setattr(spawn, "stop", lambda target: (_ for _ in ()).throw(spawn.SpawnError("no spawned agent")))
    assert bridge.deliver_inbound(order())[0] == "refused"


def test_a_stop_for_a_non_session_address_is_refused(stopped):
    assert bridge.deliver_inbound(order(to="stop:oneconv-claude-1"))[0] == "refused"


# --- over HTTP ---------------------------------------------------------------------------------------


@pytest.fixture
def paired(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "STOP_WAIT", 0)
    app = application(tmp_path / "http", ORIGIN)
    app.state.store.hub.publish("mac", [{"address": "claude:aaa", "status": "idle"}])
    with TestClient(app, base_url=ORIGIN) as client:
        token = app.state.auth.issue_pairing_token()
        client.post("/api/login", json={"token": token}, headers={"Origin": ORIGIN}).raise_for_status()
        csrf = client.get("/api/session").json()["csrf_token"]
        yield client, {"Origin": ORIGIN, "X-One-Conv-Token": csrf}


def test_the_session_offers_stopping(paired):
    client, _ = paired
    assert client.get("/api/session").json()["capabilities"]["stop"] is True


def test_a_stop_over_http_answers_its_status(paired):
    client, headers = paired
    assert client.post("/api/sessions/stop", json={"address": AGENT}, headers=headers).json()["status"] == "pending"


def test_a_stop_without_the_session_token_is_forbidden(paired):
    client, _ = paired
    assert client.post("/api/sessions/stop", json={"address": AGENT}, headers={"Origin": ORIGIN}).status_code == 403
