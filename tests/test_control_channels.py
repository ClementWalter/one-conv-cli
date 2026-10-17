"""#general membership and channels the workspace owner creates from the phone."""

import time

import pytest

from one_conv.control.store import ControlStore
from one_conv.live.hub import LEASE_SECONDS, Hub, HubError

real_time = time.time


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path)
    store.hub.publish("worker", [{"address": "omp:agent", "name": "builder", "title": "Builder", "status": "idle"},
                                 {"address": "codex:other", "name": None, "title": "Other", "status": "idle"}])
    return store


@pytest.fixture
def channel():
    return {"id": "c" * 32, "topic": "release", "purpose": "Ship 1.2 this week.\nStart with the changelog.",
            "members": ["worker/omp:agent"]}


@pytest.fixture
def created(store, channel):
    return store.create_channel(channel)


def _members(store, topic):
    return next(row["members"] for row in store.hub.topics() if row["topic"] == topic)


def _texts(store, topic):
    return [row["text"] for row in store.topic(topic)["messages"]]


# --- #general -----------------------------------------------------------------------------------


def test_a_published_session_joins_general(tmp_path):
    hub = Hub(str(tmp_path / "hub.sqlite3"), general="general")
    hub.publish("laptop", [{"address": "claude:c1"}])
    assert hub.topics() == [{"topic": "general", "members": ["laptop/claude:c1"], "last": None}]


def test_republishing_does_not_undo_leaving_general(tmp_path):
    hub = Hub(str(tmp_path / "hub.sqlite3"), general="general")
    hub.publish("laptop", [{"address": "claude:c1"}])
    hub.leave("laptop", "general", "claude:c1")
    hub.publish("laptop", [{"address": "claude:c1"}])
    assert hub.topics() == []


def test_a_plain_hub_has_no_general(tmp_path):
    hub = Hub(str(tmp_path / "hub.sqlite3"))
    hub.publish("laptop", [{"address": "claude:c1"}])
    assert hub.topics() == []


def test_the_workspace_human_is_in_general(store):
    assert "workspace/user" in _members(store, "general")


# --- creating a channel -------------------------------------------------------------------------------


def test_created_channel_holds_the_human_and_the_chosen_agents(store, created):
    assert sorted(_members(store, "release")) == ["worker/omp:agent", "workspace/user"]


def test_kickoff_is_delivered_to_the_chosen_agents(created):
    assert [row["to"] for row in created["deliveries"]] == ["worker/omp:agent"]


def test_kickoff_is_the_channels_first_message(store, created):
    assert _texts(store, "release") == ["Ship 1.2 this week.\nStart with the changelog."]


def test_general_announces_the_channel_by_member_name(store, created):
    assert _texts(store, "general")[-1].startswith("You created #release: Ship 1.2 this week.\nMembers: @builder.")


def test_general_announcement_expects_no_reply(store, created):
    assert store.topic("general")["messages"][-1]["reply"] == "none"


def test_general_announcement_reaches_agents_outside_the_channel(store, created):
    pending = {row["to"] for row in store.hub.inbox("worker", 0) if row["topic"] == "general"}
    assert pending == {"omp:agent", "codex:other"}


def test_retry_with_the_same_id_finishes_the_same_channel(store, channel, created):
    assert store.create_channel(channel)["duplicate"] is True


def test_retry_does_not_announce_twice(store, channel, created):
    store.create_channel(channel)
    assert len(_texts(store, "general")) == 1


def test_an_existing_channel_is_not_created_again(store, channel, created):
    with pytest.raises(HubError, match="already exists"):
        store.create_channel({**channel, "id": "d" * 32})


def test_general_cannot_be_created(store, channel):
    with pytest.raises(HubError, match="everyone"):
        store.create_channel({**channel, "topic": "general"})


def test_a_disconnected_agent_cannot_be_added(store, channel):
    with pytest.raises(HubError, match="not connected"):
        store.create_channel({**channel, "members": ["worker/omp:gone"]})


def test_an_agent_with_a_stale_lease_cannot_be_added(store, channel, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: real_time() + LEASE_SECONDS + 1)
    with pytest.raises(HubError, match="not connected"):
        store.create_channel(channel)


def test_a_refused_member_leaves_no_channel_behind(store, channel):
    with pytest.raises(HubError):
        store.create_channel({**channel, "members": ["worker/omp:gone"]})
    assert "release" not in {row["topic"] for row in store.hub.topics()}


def test_a_channel_can_start_with_only_the_human(store, channel):
    store.create_channel({**channel, "members": []})
    assert _members(store, "release") == ["workspace/user"]


@pytest.mark.parametrize("change", [
    {"topic": "Bad Name"}, {"purpose": "  "}, {"members": "worker/omp:agent"},
    {"members": ["worker/omp:agent", "worker/omp:agent"]}, {"members": ["workspace/user"]}, {"id": "short"},
])
def test_invalid_channel_requests_are_refused(store, channel, change):
    with pytest.raises(HubError):
        store.create_channel({**channel, **change})


def test_extra_fields_are_refused(store, channel):
    with pytest.raises(HubError, match="only id, topic, purpose and members"):
        store.create_channel({**channel, "from": "worker/omp:agent"})


# --- retries -------------------------------------------------------------------------------------------


@pytest.fixture
def interrupted(store, channel, monkeypatch):
    """The process dies after the members commit and before the kickoff is posted."""
    send = store.hub.send

    def crash(*args, **kwargs):
        raise OSError("crashed before the kickoff")

    monkeypatch.setattr(store.hub, "send", crash)
    with pytest.raises(OSError):
        store.create_channel(channel)
    monkeypatch.setattr(store.hub, "send", send)
    return store


def test_retry_after_a_crash_posts_the_kickoff(interrupted, channel):
    interrupted.create_channel(channel)
    assert _texts(interrupted, "release") == ["Ship 1.2 this week.\nStart with the changelog."]


def test_retry_after_a_crash_announces_once(interrupted, channel):
    interrupted.create_channel(channel)
    interrupted.create_channel(channel)
    assert len(_texts(interrupted, "general")) == 1


def test_retry_after_a_rename_keeps_the_first_announcement(interrupted, channel):
    interrupted.hub.publish("worker", [{"address": "omp:agent", "name": "renamed"}, {"address": "codex:other"}])
    interrupted.create_channel(channel)
    assert "@builder" in _texts(interrupted, "general")[0]


def test_same_id_with_other_members_is_refused(store, channel, created):
    with pytest.raises(HubError, match="different channel"):
        store.create_channel({**channel, "members": ["worker/omp:agent", "worker/codex:other"]})


def test_same_id_with_other_members_does_not_enlarge_the_channel(store, channel, created):
    with pytest.raises(HubError):
        store.create_channel({**channel, "members": ["worker/omp:agent", "worker/codex:other"]})
    assert "worker/codex:other" not in _members(store, "release")


# --- credential rotation --------------------------------------------------------------------------------


def test_a_revoked_hub_node_rejoins_general_when_published_again(tmp_path):
    hub = Hub(str(tmp_path / "hub.sqlite3"), general="general")
    hub.publish("laptop", [{"address": "claude:c1"}])
    hub.revoke("laptop")
    hub.publish("laptop", [{"address": "claude:c1"}])
    assert _members_of(hub, "general") == ["laptop/claude:c1"]


def test_a_revoked_workspace_node_rejoins_general_when_published_again(store):
    store.revoke_node("worker")
    store.hub.publish("worker", [{"address": "omp:agent"}])
    assert "worker/omp:agent" in _members(store, "general")


def _members_of(hub, topic):
    return next(row["members"] for row in hub.topics() if row["topic"] == topic)
