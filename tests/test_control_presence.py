"""Workspace presence reports only current publication and persisted message evidence."""

import pytest

from one_conv.control.presence import message_delivery, session_presence
from one_conv.control.store import ControlStore
from one_conv.live.hub import LEASE_SECONDS

NOW = 1_800_000_000.0


@pytest.fixture
def row():
    return {"online": True, "status": "loaded", "seen": NOW, "published_at": NOW}


@pytest.mark.parametrize("status,state", [("loaded", "unverified"), ("", "unverified"), ("unfamiliar", "unverified"),
                                        ("idle", "idle"), ("busy", "busy")])
def test_native_status_never_proves_model_availability(row, status, state):
    assert session_presence({**row, "status": status}, NOW)["state"] == state


def test_fresh_loaded_thread_remains_messageable_with_warning(row):
    assert session_presence(row, NOW)["can_message"] is True


def test_presence_last_seen_is_session_publication(row):
    assert session_presence({**row, "published_at": NOW - 10}, NOW)["last_seen"] == NOW - 10


def test_legacy_presence_falls_back_to_bridge_seen(row):
    row.pop("published_at")
    assert session_presence(row, NOW)["last_seen"] == NOW


def test_node_poll_cannot_refresh_stale_session(row):
    assert session_presence({**row, "published_at": NOW - LEASE_SECONDS - 1}, NOW)["state"] == "offline"


def test_stale_session_can_have_connected_bridge(row):
    assert session_presence({**row, "published_at": NOW - LEASE_SECONDS - 1}, NOW)["bridge_online"] is True


def test_stale_bridge_blocks_new_messages(row):
    assert session_presence({**row, "seen": NOW - LEASE_SECONDS - 1}, NOW)["can_message"] is False


def test_history_only_route_is_offline_even_with_fresh_node(row):
    assert session_presence({**row, "history_only": True}, NOW)["state"] == "offline"


def test_history_only_route_has_no_invented_session_timestamp(row):
    assert session_presence({**row, "history_only": True}, NOW)["last_seen"] is None


@pytest.mark.parametrize("value", [None, True, float("inf"), float("nan"), 0, 10**1000, -10**1000])
def test_missing_or_invalid_heartbeat_is_not_availability(row, value):
    assert session_presence({**row, "seen": value}, NOW)["can_message"] is False


@pytest.fixture
def store(tmp_path):
    active = ControlStore(tmp_path)
    active.hub.publish("worker", [{"address": "codex:agent", "name": "reviewer", "title": "Review a change", "status": "loaded"}])
    return active


def test_state_includes_evidence_based_presence(store):
    assert store.state()["sessions"][0]["presence"]["state"] == "unverified"


def test_state_does_not_hide_idle_agents(store):
    store.hub.publish("worker", [{"address": "claude:agent", "status": "idle"}])
    assert store.state()["sessions"][0]["presence"]["state"] == "idle"


def test_state_native_title_remains_task_context(store):
    assert store.state()["sessions"][0]["title"] == "Review a change"


def test_state_has_no_invented_activity_without_messages(store):
    assert store.state()["sessions"][0]["activity"]["kind"] == "unknown"


def test_state_last_received_message_is_context(store):
    store.send({"id": "a" * 32, "target": "worker/codex:agent", "text": "Review the authentication change."})
    assert store.state()["sessions"][0]["activity"]["summary"] == "Review the authentication change."


def test_state_identifies_last_outgoing_message(store):
    store.hub.send("worker", {"id": "a" * 32, "from": "codex:agent", "to": "workspace/user", "text": "I found the issue."})
    assert store.state()["sessions"][0]["activity"]["direction"] == "outgoing"


def test_disappeared_human_dm_peer_remains_visible_as_offline(store):
    store.send({"id": "a" * 32, "target": "worker/codex:agent", "text": "Review the change."})
    store.hub.publish("worker", [])
    assert store.state()["sessions"][0]["presence"]["state"] == "offline"


def test_history_only_peer_does_not_invent_an_alias(store):
    store.send({"id": "a" * 32, "target": "worker/codex:agent", "text": "Review the change."})
    store.hub.publish("worker", [])
    assert store.state()["sessions"][0]["name"] is None


def test_history_only_peer_keeps_original_route_identity(store):
    store.hub.send("worker", {"id": "a" * 32, "from": "codex:agent", "to": "workspace/user", "text": "I found the issue."})
    store.hub.publish("worker", [])
    assert store.state()["sessions"][0]["address"] == "worker/codex:agent"


@pytest.mark.parametrize("outcome", [None, "accepted", "refused", "unconfirmed"])
def test_delivery_acceptance_never_becomes_read_receipt(outcome):
    assert message_delivery(outcome, NOW)["read_at"] is None


def test_delivery_records_actual_adapter_acceptance_time():
    assert message_delivery("accepted", NOW)["accepted_at"] == NOW


def test_human_inbox_acceptance_is_labeled_storage():
    assert message_delivery("accepted", NOW, human_inbox=True)["label"] == "Stored in your inbox"
