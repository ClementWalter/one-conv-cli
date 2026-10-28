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
"""One work channel per session: a post wakes the sessions working in its channel, watchers only get an fyi.

The first channel a session joins is its work channel; `work=True` moves it, `work=False` only
watches. A human post in a channel where nobody works starts a session for it, even when watchers
are running there. Archived channels leave the human's list and stay readable.
"""

import sqlite3

import pytest

from one_conv.control.auth import AuthStore
from one_conv.control.store import ControlStore
from one_conv.live.hub import Hub, HubError

HUMAN = "workspace"


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    state.publish(HUMAN, [{"address": "user"}])
    state.adopt_trusted_human(HUMAN)
    state.publish("mac", [{"address": "claude:builder"}, {"address": "claude:busy"}])
    state.join("mac", "release", "claude:builder")
    state.join("mac", "defi", "claude:busy")
    state.join("mac", "release", "claude:busy")
    return state


def post(hub, ident="a", to="#release"):
    hub.send(HUMAN, {"id": ident * 32, "from": "user", "to": to, "text": "Ship it", "reply": "expected",
                     "mode": "push"}, join_sender=True)
    return {item["to"]: item["mode"] for item in hub.inbox("mac", 0)}


def topic(hub, name):
    return next(entry for entry in hub.topics() if entry["topic"] == name)


def test_the_first_channel_joined_is_the_work_channel(hub):
    assert topic(hub, "defi")["working"] == ["mac/claude:busy"]


def test_a_later_channel_is_only_watched(hub):
    assert topic(hub, "release")["working"] == ["mac/claude:builder"]


def test_a_post_wakes_the_session_working_in_the_channel(hub):
    assert post(hub)["claude:builder"] == "push"


def test_a_post_reaches_a_watcher_without_waking_it(hub):
    assert post(hub)["claude:busy"] == "fyi"


def test_an_agent_post_reaches_a_watcher_without_waking_it(hub):
    hub.send("mac", {"id": "b" * 32, "from": "claude:builder", "to": "#release", "text": "I take it"})
    assert {item["to"]: item["mode"] for item in hub.inbox("mac", 0)}["claude:busy"] == "fyi"


def test_joining_with_work_moves_the_work_channel(hub):
    hub.join("mac", "release", "claude:busy", work=True)
    assert topic(hub, "defi")["working"] == []


def test_joining_with_work_wakes_the_session_there(hub):
    hub.join("mac", "release", "claude:busy", work=True)
    assert post(hub)["claude:busy"] == "push"


def test_joining_to_watch_keeps_the_work_channel(hub):
    hub.join("mac", "defi", "claude:busy", work=False)
    assert topic(hub, "defi")["working"] == []


def test_running_counts_only_working_sessions(hub):
    assert topic(hub, "release")["running"] == 1


def test_a_channel_with_only_watchers_starts_a_session(hub):
    hub.publish("mac", [{"address": "claude:builder"}, {"address": "claude:busy"}], spawn="claude")
    hub.join("mac", "defi-watch", "claude:builder", work=False)
    post(hub, ident="c", to="#defi-watch")
    with sqlite3.connect(hub.path) as db:
        assert db.execute("SELECT route FROM staffing WHERE task = ?", (f"{HUMAN}:{'c' * 32}",)).fetchone() == (
            "mac/spawn:claude",)


def test_memberships_from_before_work_channels_only_watch(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE members (topic TEXT NOT NULL, node TEXT NOT NULL, address TEXT NOT NULL,"
                   " joined REAL NOT NULL, PRIMARY KEY (topic, node, address))")
        db.execute("INSERT INTO members VALUES ('general', 'mac', 'claude:old', 1.0)")
    Hub(path).publish("mac", [{"address": "claude:old"}])
    assert topic(Hub(path), "general")["working"] == []


# --- archived channels ------------------------------------------------------------------------------


def test_an_archived_channel_is_marked_archived(hub):
    hub.archive("release", True)
    assert topic(hub, "release")["archived"] is True


def test_an_archived_channel_stays_readable(hub):
    post(hub)
    hub.archive("release", True)
    assert [row["text"] for row in hub.history("release")] == ["Ship it"]


def test_bringing_a_channel_back_unarchives_it(hub):
    hub.archive("release", True)
    hub.archive("release", False)
    assert topic(hub, "release")["archived"] is False


def test_a_human_post_brings_an_archived_channel_back(hub):
    hub.archive("release", True)
    post(hub)
    assert topic(hub, "release")["archived"] is False


def test_an_agent_post_keeps_a_channel_archived(hub):
    hub.archive("release", True)
    hub.send("mac", {"id": "d" * 32, "from": "claude:builder", "to": "#release", "text": "Still here"})
    assert topic(hub, "release")["archived"] is True


def test_archiving_an_invalid_channel_is_refused(hub):
    with pytest.raises(HubError):
        hub.archive("Not A Topic", True)


@pytest.fixture
def store(tmp_path):
    return ControlStore(tmp_path / "store")


@pytest.mark.parametrize("payload", [{"topic": "release"}, {"topic": "release", "archived": "yes"},
                                     {"topic": "BAD", "archived": True}, {"topic": "release", "archived": True, "x": 1}],
                         ids=["missing", "not-bool", "bad-topic", "extra"])
def test_an_invalid_archive_request_is_refused(store, payload):
    with pytest.raises(HubError):
        store.archive_channel(payload)


def test_archiving_changes_the_workspace_fingerprint(store):
    before = store.fingerprint()
    store.archive_channel({"topic": "general", "archived": True})
    assert store.fingerprint() != before


# --- signing out other devices ------------------------------------------------------------------


@pytest.fixture
def devices(tmp_path):
    auth = AuthStore(tmp_path)
    phone = auth.pair(auth.issue_pairing_token())
    laptop = auth.pair(auth.issue_pairing_token())
    pending = auth.issue_pairing_token()
    return auth, phone, laptop, pending


def test_signing_out_others_counts_them(devices):
    auth, phone, _, _ = devices
    assert auth.revoke_others(phone) == 1


def test_signing_out_others_disconnects_the_other_device(devices):
    auth, phone, laptop, _ = devices
    auth.revoke_others(phone)
    assert auth.session(laptop) is None


def test_signing_out_others_keeps_this_device(devices):
    auth, phone, _, _ = devices
    auth.revoke_others(phone)
    assert auth.session(phone) is not None


def test_signing_out_others_cancels_an_unused_pairing_link(devices):
    auth, phone, _, pending = devices
    auth.revoke_others(phone)
    assert auth.pair(pending) is None
