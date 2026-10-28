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
"""From no running session to one: the hub staffs a human post at once, on a connected machine first.

A human post in a channel where no session runs gets a staffing record in the same commit: a
spawn order to a bridge that offers to start sessions, or the cloud when none is connected. A
spawn order that is refused, or that no ack follows in time, moves to the cloud. An ack closes
the record. The Mac's bridge starts the session in tmux; the control service submits one cloud
worker per staffing record.
"""

import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from one_conv.control import runtime
from one_conv.control.runtime import manage_spawner, staff_from_cloud
from one_conv.live import bridge, hubclient, spawn, workspace
from one_conv.live import hub as hub_module
from one_conv.live.bridge_cli import add_bridge_commands
from one_conv.live.hub import Hub, HubError, application
from tests.test_live_bridge import serve_app

HUMAN = "workspace"


def post(hub, ident="a", to="#work", text="Fix the build"):
    """The human posts; returns the staffing key of the post."""
    hub.send(HUMAN, {"id": ident * 32, "from": "user", "to": to, "text": text, "reply": "expected", "mode": "push"},
             join_sender=to.startswith("#"))
    return f"{HUMAN}:{ident * 32}"


def staffing(hub, key):
    with sqlite3.connect(hub.path) as db:
        return db.execute("SELECT route, state FROM staffing WHERE task = ?", (key,)).fetchone()


def spawn_orders(hub, node="mac"):
    return [item for item in hub.inbox(node, 0) if item["to"].startswith("spawn:")]


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    state.publish(HUMAN, [{"address": "user"}])
    state.adopt_trusted_human(HUMAN)
    return state


@pytest.fixture
def spawner(hub):
    """The Mac's bridge is connected, runs no session, and offers to start claude sessions."""
    hub.publish("mac", [], spawn="claude")
    return hub


def started_and_acked(hub, key):
    """The session the Mac started registers, joins and acks the post."""
    hub.publish("mac", [{"address": "claude:new"}], spawn="claude")
    hub.join("mac", "work", "claude:new")
    hub.acknowledge("mac", "claude:new", key.partition(":")[2])


def test_a_post_with_nobody_anywhere_goes_to_the_cloud(hub):
    assert staffing(hub, post(hub)) == ("cloud", "cloud")


def test_a_post_with_a_connected_spawner_goes_to_that_machine(spawner):
    assert staffing(spawner, post(spawner)) == ("mac/spawn:claude", "node")


def test_the_spawner_receives_a_spawn_order(spawner):
    post(spawner)
    assert [item["to"] for item in spawn_orders(spawner)] == ["spawn:claude"]


def test_the_spawn_order_wakes_the_spawner(spawner):
    post(spawner)
    assert spawn_orders(spawner)[0]["mode"] == "push"


def test_the_spawn_order_tells_the_session_to_ack(spawner):
    key = post(spawner)
    assert f'message_id="{key.partition(":")[2]}"' in spawn_orders(spawner)[0]["text"]


def test_the_spawn_order_carries_the_post(spawner):
    post(spawner, text="Rename the flag")
    assert "Rename the flag" in spawn_orders(spawner)[0]["text"]


def test_the_spawn_order_is_not_in_the_channel_history(spawner):
    post(spawner)
    assert [row["text"] for row in spawner.history("work")] == ["Fix the build"]


def test_a_post_with_a_running_member_is_not_staffed(hub):
    hub.publish("mac", [{"address": "claude:one"}], spawn="claude")
    hub.join("mac", "work", "claude:one")
    assert staffing(hub, post(hub)) is None


def test_a_running_member_republishing_does_not_staff_the_post(hub, monkeypatch):
    hub.publish("mac", [{"address": "claude:one"}], spawn="claude")
    hub.join("mac", "work", "claude:one")
    key = post(hub)
    # Every clock read is later than the previous one, as on a real machine.
    start, ticks = time.time(), iter(range(10**9))
    monkeypatch.setattr(hub_module.time, "time", lambda: start + next(ticks) * 0.001)
    hub.publish("mac", [{"address": "claude:one"}], spawn="claude")
    assert staffing(hub, key) is None


def test_an_offline_spawner_is_skipped(spawner):
    with sqlite3.connect(spawner.path) as db:
        db.execute("UPDATE nodes SET seen = ? WHERE node = 'mac'", (time.time() - hub_module.LEASE_SECONDS - 1,))
    assert staffing(spawner, post(spawner)) == ("cloud", "cloud")


def test_a_bridge_that_stops_offering_is_no_longer_a_spawner(spawner):
    spawner.publish("mac", [])
    assert staffing(spawner, post(spawner)) == ("cloud", "cloud")


def test_an_unknown_spawn_harness_is_refused(hub):
    with pytest.raises(HubError):
        hub.publish("mac", [], spawn="vim")


def test_a_refused_spawn_moves_to_the_cloud(spawner):
    key = post(spawner)
    spawner.ack("mac", spawn_orders(spawner)[0]["delivery"], "refused")
    spawner.sweep()
    assert staffing(spawner, key) == ("cloud", "cloud")


def test_a_spawn_that_nobody_acks_in_time_is_retried_on_the_same_spawner(spawner, monkeypatch):
    key = post(spawner)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    spawner.sweep()
    assert staffing(spawner, key) == ("mac/spawn:claude", "node")


def test_a_retried_spawn_is_a_new_order(spawner, monkeypatch):
    post(spawner)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    spawner.sweep()
    assert len({item["id"] for item in spawn_orders(spawner)}) == 2


def test_a_spawn_order_never_taken_is_retired_when_retried(spawner, monkeypatch):
    post(spawner)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    spawner.sweep()
    assert [item["mode"] for item in spawn_orders(spawner)] == ["fyi", "push"]


def test_after_spawn_attempts_orders_the_request_goes_to_the_cloud(spawner, monkeypatch):
    key = post(spawner)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    for _ in range(hub_module.SPAWN_ATTEMPTS):
        spawner.sweep()
    assert staffing(spawner, key) == ("cloud", "cloud")


def test_a_spawner_that_refused_is_not_retried(spawner, monkeypatch):
    key = post(spawner)
    spawner.ack("mac", spawn_orders(spawner)[0]["delivery"], "refused")
    spawner.sweep()
    spawner.publish("mac", [], spawn="claude")
    assert staffing(spawner, key) == ("cloud", "cloud")


def test_a_spawner_back_online_retries_a_request_it_failed(spawner, monkeypatch):
    key = post(spawner)
    with sqlite3.connect(spawner.path) as db:
        db.execute("UPDATE nodes SET seen = ? WHERE node = 'mac'", (time.time() - hub_module.LEASE_SECONDS - 1,))
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    spawner.sweep()
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", 90.0)
    spawner.publish("mac", [], spawn="claude")
    assert staffing(spawner, key) == ("mac/spawn:claude", "node")


def test_an_ack_closes_the_staffing(spawner):
    key = post(spawner)
    started_and_acked(spawner, key)
    spawner.sweep()
    assert staffing(spawner, key) == ("mac/spawn:claude", "done")


def test_a_spawn_order_handed_out_after_an_ack_starts_nothing(spawner):
    key = post(spawner)
    started_and_acked(spawner, key)
    spawner.sweep()
    assert spawn_orders(spawner)[0]["mode"] == "fyi"


# --- one channel, one session being started ------------------------------------------------------

def test_a_second_post_while_the_channel_is_being_staffed_starts_nothing(spawner):
    post(spawner, "a")
    key = post(spawner, "b", text="Wtf")
    assert staffing(spawner, key) is None


def test_a_second_post_while_the_channel_is_being_staffed_sends_one_spawn_order(spawner):
    post(spawner, "a")
    post(spawner, "b", text="Wtf")
    assert len(spawn_orders(spawner)) == 1


def test_a_post_in_another_channel_is_staffed_meanwhile(spawner):
    post(spawner, "a")
    assert staffing(spawner, post(spawner, "b", to="#other")) == ("mac/spawn:claude", "node")


def test_a_stale_staffing_record_does_not_block_its_channel(spawner, monkeypatch):
    post(spawner, "a")
    with sqlite3.connect(spawner.path) as db:
        db.execute("UPDATE staffing SET state = 'cloud', created = ?", (time.time() - hub_module.STAFF_WINDOW_SECONDS - 1,))
    assert staffing(spawner, post(spawner, "b")) == ("mac/spawn:claude", "node")


def test_a_session_that_joined_to_work_closes_the_staffing_without_an_ack(spawner):
    key = post(spawner)
    spawner.publish("mac", [{"address": "claude:new"}], spawn="claude")
    spawner.join("mac", "work", "claude:new", work=True)
    spawner.sweep()
    assert staffing(spawner, key) == ("mac/spawn:claude", "done")


def test_a_session_that_joined_to_work_stops_the_retries(spawner, monkeypatch):
    post(spawner)
    spawner.publish("mac", [{"address": "claude:new"}], spawn="claude")
    spawner.join("mac", "work", "claude:new", work=True)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    spawner.sweep()
    assert len(spawn_orders(spawner)) == 1


# --- a teammate request: one more session in a channel -------------------------------------------

def teammate(hub, ident="c", topic="work", text="Defend the flat-earth side"):
    return hub.request_teammate(HUMAN, "user", ident * 32, topic, text)


def test_a_teammate_request_goes_to_a_spawner(spawner):
    assert teammate(spawner)["route"] == "mac/spawn:claude"


def test_a_teammate_request_is_staffed_even_with_a_running_member(spawner):
    spawner.publish("mac", [{"address": "claude:one"}], spawn="claude")
    spawner.join("mac", "work", "claude:one", work=True)
    assert teammate(spawner)["status"] == "node"


def test_a_teammate_request_does_not_wake_the_running_member(spawner):
    spawner.publish("mac", [{"address": "claude:one"}], spawn="claude")
    spawner.join("mac", "work", "claude:one", work=True)
    teammate(spawner)
    assert [item["mode"] for item in spawner.inbox("mac", 0) if item["to"] == "claude:one"] == ["fyi"]


def test_a_teammate_request_is_shown_in_the_channel(spawner):
    teammate(spawner)
    assert [row["text"] for row in spawner.history("work")] == ["Defend the flat-earth side"]


def test_the_teammate_spawn_order_tells_the_session_to_join_the_others(spawner):
    teammate(spawner)
    assert "asked for one more session in #work" in spawn_orders(spawner)[0]["text"]


def test_the_teammate_spawn_order_carries_the_request(spawner):
    teammate(spawner)
    assert "Defend the flat-earth side" in spawn_orders(spawner)[0]["text"]


def test_a_teammate_request_is_not_closed_by_a_running_member(spawner):
    spawner.publish("mac", [{"address": "claude:one"}], spawn="claude")
    spawner.join("mac", "work", "claude:one", work=True)
    teammate(spawner)
    spawner.sweep()
    assert staffing(spawner, f"{HUMAN}:{'c' * 32}") == ("mac/spawn:claude", "node")


def test_an_ack_closes_a_teammate_request(spawner):
    teammate(spawner)
    started_and_acked(spawner, f"{HUMAN}:{'c' * 32}")
    spawner.sweep()
    assert staffing(spawner, f"{HUMAN}:{'c' * 32}") == ("mac/spawn:claude", "done")


def test_a_retried_teammate_request_sends_one_spawn_order(spawner):
    teammate(spawner)
    teammate(spawner)
    assert len(spawn_orders(spawner)) == 1


def test_a_teammate_request_reusing_an_id_for_other_text_is_refused(spawner):
    teammate(spawner)
    with pytest.raises(HubError):
        teammate(spawner, text="Something else")


def test_two_teammate_requests_in_one_channel_start_two_sessions(spawner):
    teammate(spawner, "c")
    teammate(spawner, "d", text="Defend the globe side")
    assert len(spawn_orders(spawner)) == 2


# --- several spawners: the preferred first, the next one when it fails --------------------------


@pytest.fixture
def two_spawners(hub):
    """The Mac (preferred) and a cloud box are both connected and offer to start claude sessions."""
    hub.publish("cloudbox", [], spawn="claude", priority=0)
    hub.publish("mac", [], spawn="claude", priority=10)
    return hub


def test_the_preferred_spawner_is_asked_first(two_spawners):
    assert staffing(two_spawners, post(two_spawners)) == ("mac/spawn:claude", "node")


def test_the_preferred_spawner_wins_even_if_another_polled_later(two_spawners):
    two_spawners.publish("cloudbox", [], spawn="claude", priority=0)
    assert staffing(two_spawners, post(two_spawners)) == ("mac/spawn:claude", "node")


def test_with_the_preferred_spawner_offline_the_other_is_asked(two_spawners):
    with sqlite3.connect(two_spawners.path) as db:
        db.execute("UPDATE nodes SET seen = ? WHERE node = 'mac'", (time.time() - hub_module.LEASE_SECONDS - 1,))
    assert staffing(two_spawners, post(two_spawners)) == ("cloudbox/spawn:claude", "node")


def test_a_spawner_whose_bridge_stopped_is_skipped_even_if_its_node_still_calls(two_spawners):
    with sqlite3.connect(two_spawners.path) as db:
        db.execute("UPDATE spawners SET updated = ? WHERE node = 'mac'", (time.time() - hub_module.LEASE_SECONDS - 1,))
    assert staffing(two_spawners, post(two_spawners)) == ("cloudbox/spawn:claude", "node")


def test_a_refused_spawn_moves_to_the_next_spawner(two_spawners):
    key = post(two_spawners)
    two_spawners.ack("mac", spawn_orders(two_spawners)[0]["delivery"], "refused")
    two_spawners.sweep()
    assert staffing(two_spawners, key) == ("cloudbox/spawn:claude", "node")


def test_the_next_spawner_receives_its_own_spawn_order(two_spawners):
    post(two_spawners)
    two_spawners.ack("mac", spawn_orders(two_spawners)[0]["delivery"], "refused")
    two_spawners.sweep()
    assert [item["to"] for item in spawn_orders(two_spawners, "cloudbox")] == ["spawn:claude"]


def test_a_spawn_nobody_acks_in_time_moves_to_the_next_spawner(two_spawners, monkeypatch):
    key = post(two_spawners)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    two_spawners.sweep()
    assert staffing(two_spawners, key) == ("cloudbox/spawn:claude", "node")


def test_when_every_spawner_failed_the_preferred_one_is_retried(two_spawners, monkeypatch):
    key = post(two_spawners)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    two_spawners.sweep()
    two_spawners.sweep()
    assert staffing(two_spawners, key) == ("mac/spawn:claude", "node")


def test_when_every_spawner_failed_spawn_attempts_times_the_request_goes_to_the_cloud(two_spawners, monkeypatch):
    key = post(two_spawners)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    for _ in range(hub_module.SPAWN_ATTEMPTS):
        two_spawners.sweep()
    assert staffing(two_spawners, key) == ("cloud", "cloud")


def test_an_ack_after_a_fallback_silences_every_spawn_order(two_spawners, monkeypatch):
    key = post(two_spawners)
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    two_spawners.sweep()
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", 90.0)
    started_and_acked(two_spawners, key)
    two_spawners.sweep()
    assert [item["mode"] for item in spawn_orders(two_spawners, "cloudbox")] == ["fyi"]


def test_a_spawner_that_connects_later_takes_a_waiting_request(hub):
    key = post(hub)
    hub.publish("cloudbox", [], spawn="claude")
    assert staffing(hub, key) == ("cloudbox/spawn:claude", "node")


def test_a_spawner_that_connects_later_skips_an_old_request(hub, monkeypatch):
    key = post(hub)
    monkeypatch.setattr(hub_module, "STAFF_WINDOW_SECONDS", -1.0)
    hub.publish("cloudbox", [], spawn="claude")
    assert staffing(hub, key) == ("cloud", "cloud")


def test_a_non_integer_spawn_priority_is_refused(hub):
    with pytest.raises(HubError):
        hub.publish("mac", [], spawn="claude", priority="high")


def test_the_spawn_order_names_the_session_that_worked_in_the_channel(spawner):
    spawner.publish("oldmac", [{"address": "claude:old"}])
    spawner.send("oldmac", {"id": "c" * 32, "from": "claude:old", "to": "#work", "text": "halfway there",
                            "reply": "none", "mode": "push"}, join_sender=True)
    spawner.publish("oldmac", [])  # Its machine went away.
    post(spawner)
    assert "oldmac/claude:old" in spawn_orders(spawner)[0]["text"]


def test_the_spawn_order_tells_the_session_to_read_its_predecessor(spawner):
    spawner.publish("oldmac", [{"address": "claude:old"}])
    spawner.send("oldmac", {"id": "c" * 32, "from": "claude:old", "to": "#work", "text": "halfway there",
                            "reply": "none", "mode": "push"}, join_sender=True)
    spawner.publish("oldmac", [])  # Its machine went away.
    post(spawner)
    assert "Take over their work" in spawn_orders(spawner)[0]["text"]


def test_a_fresh_channel_has_no_takeover_section(spawner):
    post(spawner)
    assert "Take over" not in spawn_orders(spawner)[0]["text"]


def test_the_spawn_order_tells_the_session_to_push(spawner):
    post(spawner)
    assert "commit and push every change" in spawn_orders(spawner)[0]["text"]


def test_an_old_post_is_not_staffed(hub, monkeypatch):
    monkeypatch.setattr(hub_module, "STAFF_WINDOW_SECONDS", -1.0)
    hub.publish("mac", [{"address": "claude:one"}])
    hub.join("mac", "work", "claude:one")
    key = post(hub)
    hub.publish("mac", [])
    hub.sweep()
    assert staffing(hub, key) is None


def test_a_direct_post_is_not_staffed(hub):
    hub.publish("mac", [{"address": "claude:one"}])
    assert staffing(hub, post(hub, to="mac/claude:one")) is None


def test_an_announcement_is_not_staffed(hub):
    hub.send(HUMAN, {"id": "b" * 32, "from": "user", "to": "#work", "text": "FYI", "reply": "none", "mode": "push"},
             join_sender=True)
    assert staffing(hub, f"{HUMAN}:{'b' * 32}") is None


# --- the control service submits cloud workers ---------------------------------------------------


class StubManager:
    """Records submitted worker specs; a spec under a known job id returns that job."""

    def __init__(self):
        self.jobs = {}

    def submit(self, spec, id):
        self.jobs.setdefault(id, {"id": id, "spec": spec})
        return self.jobs[id]


@pytest.fixture
def workers():
    return SimpleNamespace(manager=StubManager(), rate_card=lambda job: None)


def test_the_cloud_staffing_submits_one_worker(hub, workers):
    post(hub)
    staff_from_cloud(hub, workers)
    assert len(workers.manager.jobs) == 1


def test_the_cloud_worker_joins_the_post_channel(hub, workers):
    post(hub)
    job, = staff_from_cloud(hub, workers)
    assert job["spec"]["topic"] == "work"


def test_the_cloud_worker_gets_the_ack_instructions(hub, workers):
    key = post(hub)
    job, = staff_from_cloud(hub, workers)
    assert f'message_id="{key.partition(":")[2]}"' in job["spec"]["prompt"]


def test_a_submitted_post_is_not_submitted_again(hub, workers):
    post(hub)
    staff_from_cloud(hub, workers)
    assert staff_from_cloud(hub, workers) == []


def test_the_cloud_staffing_is_marked_submitted(hub, workers):
    key = post(hub)
    staff_from_cloud(hub, workers)
    assert staffing(hub, key) == ("cloud", "submitted")


def test_without_cloud_workers_the_post_is_marked_unavailable(hub):
    key = post(hub)
    staff_from_cloud(hub, None)
    assert staffing(hub, key) == ("cloud", "unavailable")


class FailingManager:
    def submit(self, spec, id):
        raise ValueError("worker limit reached")


@pytest.fixture
def unavailable(hub):
    """The cloud refused the request's worker once."""
    key = post(hub)
    staff_from_cloud(hub, SimpleNamespace(manager=FailingManager(), rate_card=lambda job: None))
    return key


def test_an_unavailable_request_waits_before_its_retry(hub, unavailable):
    hub.sweep()
    assert staffing(hub, unavailable) == ("cloud", "unavailable")


def test_an_unavailable_request_goes_back_to_the_cloud(hub, unavailable, monkeypatch):
    monkeypatch.setattr(hub_module, "UNAVAILABLE_RETRY_SECONDS", -1.0)
    hub.sweep()
    assert staffing(hub, unavailable) == ("cloud", "cloud")


def test_an_unavailable_request_is_submitted_again(hub, unavailable, workers, monkeypatch):
    monkeypatch.setattr(hub_module, "UNAVAILABLE_RETRY_SECONDS", -1.0)
    hub.sweep()
    staff_from_cloud(hub, workers)
    assert staffing(hub, unavailable) == ("cloud", "submitted")


def test_an_old_unavailable_request_is_not_retried(hub, unavailable, monkeypatch):
    monkeypatch.setattr(hub_module, "UNAVAILABLE_RETRY_SECONDS", -1.0)
    monkeypatch.setattr(hub_module, "STAFF_WINDOW_SECONDS", -1.0)
    hub.sweep()
    assert staffing(hub, unavailable) == ("cloud", "unavailable")


def test_an_ack_closes_an_unavailable_request(hub, unavailable):
    hub.publish("mac", [{"address": "claude:new"}])
    hub.join("mac", "work", "claude:new")
    hub.acknowledge("mac", "claude:new", unavailable.partition(":")[2])
    hub.sweep()
    assert staffing(hub, unavailable) == ("cloud", "done")


# --- the control service runs the cloud spawner's box only while there is work -----------------


class StubBoat:
    """Records Boat calls against one sandbox in a given machine state."""

    def __init__(self, state, snapshot="2026-10-26T08:00:00Z"):
        self.state, self.snapshot, self.calls = state, snapshot, []

    def info(self, box):
        self.calls.append(("info", box))
        return {"id": box, "state": self.state, "snapshotCompletedAt": self.snapshot}

    def resume_pool(self, box):
        self.calls.append(("resume", box))

    def stop_pool(self, box):
        self.calls.append(("stop", box))

    def _exec(self, box, command):
        self.calls.append(("exec", box, tuple(command)))
        return ""


CLOUD_SPAWNER = {"box": "bx_cloud", "node": "cloudbox"}
IDLE = {"active": time.time() - runtime.SPAWNER_IDLE_SECONDS - 1}


@pytest.fixture
def cloud_online(hub):
    """The cloud box's bridge is connected and offers to start sessions; nothing runs on it."""
    hub.publish("cloudbox", [], spawn="claude")
    return hub


def test_an_online_cloud_spawner_with_recent_work_is_left_alone(cloud_online):
    boat = StubBoat("idle")
    manage_spawner(cloud_online, boat, CLOUD_SPAWNER, {"active": time.time()})
    assert boat.calls == []


def test_an_idle_cloud_spawner_box_is_stopped(cloud_online):
    boat = StubBoat("idle")
    manage_spawner(cloud_online, boat, CLOUD_SPAWNER, dict(IDLE))
    assert boat.calls == [("stop", "bx_cloud")]


def test_a_stopping_cloud_spawner_gets_no_spawn_order(cloud_online):
    manage_spawner(cloud_online, StubBoat("idle"), CLOUD_SPAWNER, dict(IDLE))
    cloud_online.publish("cloudbox", [], spawn="claude")
    assert staffing(cloud_online, post(cloud_online)) == ("cloud", "cloud")


def test_a_busy_session_keeps_the_box_running(hub):
    hub.publish("cloudbox", [{"address": "claude:a", "status": "busy"}], spawn="claude")
    boat = StubBoat("idle")
    manage_spawner(hub, boat, CLOUD_SPAWNER, dict(IDLE))
    assert boat.calls == []


def test_an_idle_session_does_not_keep_the_box_running(hub):
    hub.publish("cloudbox", [{"address": "claude:a", "status": "idle"}], spawn="claude")
    boat = StubBoat("idle")
    manage_spawner(hub, boat, CLOUD_SPAWNER, dict(IDLE))
    assert boat.calls == [("stop", "bx_cloud")]


def test_a_spawn_order_in_flight_keeps_the_box_running(cloud_online):
    post(cloud_online)
    boat = StubBoat("idle")
    manage_spawner(cloud_online, boat, CLOUD_SPAWNER, dict(IDLE))
    assert boat.calls == []


def test_the_idle_clock_starts_when_the_controller_first_sees_the_box(cloud_online):
    boat = StubBoat("idle")
    manage_spawner(cloud_online, boat, CLOUD_SPAWNER, {})
    assert boat.calls == []


def test_a_sleeping_box_with_no_request_stays_asleep(hub):
    boat = StubBoat("stopped")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert ("resume", "bx_cloud") not in boat.calls


def test_a_request_nobody_can_take_wakes_the_box(hub):
    post(hub)
    boat = StubBoat("stopped")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert ("resume", "bx_cloud") in boat.calls


def test_a_woken_box_keeps_the_bridge_it_resumes_with(hub):
    post(hub)
    boat = StubBoat("stopped")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert [call for call in boat.calls if call[0] == "exec"] == []


def test_a_running_box_with_a_dead_bridge_gets_it_restarted(hub):
    post(hub)
    boat = StubBoat("idle")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert boat.calls[-1] == ("exec", "bx_cloud", ("systemctl", "--user", "restart", "one-conv-bridge.service"))


def test_a_woken_box_takes_spawn_orders_again(cloud_online):
    manage_spawner(cloud_online, StubBoat("idle"), CLOUD_SPAWNER, dict(IDLE))
    key = post(cloud_online)
    manage_spawner(cloud_online, StubBoat("stopped"), CLOUD_SPAWNER, {})
    cloud_online.publish("cloudbox", [], spawn="claude")
    assert staffing(cloud_online, key) == ("cloudbox/spawn:claude", "node")


def test_a_running_box_with_a_dead_bridge_is_not_resumed(hub):
    post(hub)
    boat = StubBoat("idle")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert ("resume", "bx_cloud") not in boat.calls


def test_a_request_the_box_failed_once_wakes_it_again(hub):
    key = post(hub)
    with sqlite3.connect(hub.path) as db:
        db.execute("UPDATE staffing SET tried = 'cloudbox' WHERE task = ?", (key,))
    boat = StubBoat("stopped")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert ("resume", "bx_cloud") in boat.calls


def test_a_request_the_box_failed_spawn_attempts_times_does_not_wake_it(hub):
    key = post(hub)
    with sqlite3.connect(hub.path) as db:
        db.execute("UPDATE staffing SET tried = 'cloudbox cloudbox cloudbox' WHERE task = ?", (key,))
    boat = StubBoat("stopped")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert ("resume", "bx_cloud") not in boat.calls


def test_a_recent_wake_attempt_is_not_repeated(hub):
    post(hub)
    boat = StubBoat("stopped")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {"tried": time.time()})
    assert boat.calls == []


def test_a_box_asleep_for_a_week_is_woken_to_refresh_its_snapshot(hub):
    boat = StubBoat("stopped", snapshot="2026-01-01T00:00:00Z")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {})
    assert ("resume", "bx_cloud") in boat.calls


def test_a_sleeping_box_is_checked_at_most_hourly(hub):
    boat = StubBoat("stopped", snapshot="2026-01-01T00:00:00Z")
    manage_spawner(hub, boat, CLOUD_SPAWNER, {"checked": time.time()})
    assert boat.calls == []


# --- the Mac's bridge starts the session --------------------------------------------------------


@pytest.fixture
def started(tmp_path, monkeypatch):
    """Spawn settings for claude in tmp_path; spawn.start calls are recorded instead of run."""
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    calls = []
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(workspace, "pull", lambda repository: False)
    monkeypatch.setattr(spawn, "start", lambda *args, **kwargs: calls.append((args, kwargs))
                        or spawn.Spawned("oneconv-claude-1", "claude", str(tmp_path), "claude:new"))
    bridge.save_spawn_settings({"harness": "claude", "cwd": str(tmp_path), "model": None,
                                "args": ["--dangerously-skip-permissions"]})
    return calls


def wait_for(calls):
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.02)
    return calls


def order(mode="push"):
    return {"to": "spawn:claude", "mode": mode, "text": "Ack message workspace:x first", "delivery": uuid.uuid4().hex}


def test_a_spawn_order_starts_a_session(started):
    bridge.deliver_inbound(order())
    assert wait_for(started)[0][0][1] == "Ack message workspace:x first"


def test_a_started_session_gets_the_configured_arguments(started):
    bridge.deliver_inbound(order())
    assert wait_for(started)[0][0][4] == ("--dangerously-skip-permissions",)


def test_a_spawn_order_is_accepted(started):
    outcome = bridge.deliver_inbound(order())[0]
    wait_for(started)  # The start thread must not outlive this test's stubs.
    assert outcome == "accepted"


def test_a_spawn_order_after_an_ack_starts_nothing(started):
    bridge.deliver_inbound(order("fyi"))
    time.sleep(0.2)
    assert started == []


def test_a_spawn_order_is_refused_while_the_env_file_is_missing(started, tmp_path):
    bridge.save_spawn_settings({**bridge.spawn_settings(), "env_file": str(tmp_path / "absent.sh")})
    assert bridge.deliver_inbound(order())[0] == "refused"


def test_a_started_session_gets_the_env_file_variables(started, tmp_path):
    (tmp_path / "env.sh").write_text("export CLAUDE_CODE_OAUTH_TOKEN=fresh\n")
    bridge.save_spawn_settings({**bridge.spawn_settings(), "env_file": str(tmp_path / "env.sh")})
    bridge.deliver_inbound(order())
    assert wait_for(started)[0][1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fresh"


def test_a_spawn_order_without_settings_is_refused(started):
    bridge.save_spawn_settings(None)
    assert bridge.deliver_inbound(order())[0] == "refused"


def test_a_spawn_order_for_another_harness_is_refused(started):
    assert bridge.deliver_inbound({**order(), "to": "spawn:codex"})[0] == "refused"


def test_a_missing_tmux_refuses_the_spawn_order(started, monkeypatch):
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: (_ for _ in ()).throw(spawn.SpawnError("no tmux")))
    assert bridge.deliver_inbound(order())[0] == "refused"


def test_the_bridge_offers_to_spawn_over_http(started, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.router, "list_all", lambda harness=None: [])
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    try:
        client = hubclient.HubClient(url, state.add_token("mac"), "mac")
        bridge.Bridge(client, "mac", bridge.Ingress(tmp_path / "ingress.db")).publish()
        state.publish(HUMAN, [{"address": "user"}])
        state.adopt_trusted_human(HUMAN)
        assert staffing(state, post(state)) == ("mac/spawn:claude", "node")
    finally:
        stop()


def test_a_bridge_step_turns_the_order_into_a_started_session(started, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.router, "list_all", lambda harness=None: [])
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    try:
        client = hubclient.HubClient(url, state.add_token("mac"), "mac")
        node = bridge.Bridge(client, "mac", bridge.Ingress(tmp_path / "ingress.db"))
        node.publish()
        state.publish(HUMAN, [{"address": "user"}])
        state.adopt_trusted_human(HUMAN)
        post(state)
        node.step(wait=0)
        assert len(wait_for(started)) == 1
    finally:
        stop()


# --- the spawner settings command ---------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    import click
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    group = click.Group("live")
    add_bridge_commands(group)
    return CliRunner(), group


def test_the_spawner_command_saves_with_yes(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path), "--yes"])
    assert bridge.spawn_settings()["cwd"] == str(tmp_path.resolve())


def test_the_spawner_command_saves_nothing_without_yes(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path)])
    assert bridge.spawn_settings() is None


def test_the_spawner_command_turns_spawning_off(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path), "--yes"])
    runner.invoke(group, ["spawner", "--off", "--yes"])
    assert bridge.spawn_settings() is None


def test_the_spawner_command_needs_harness_and_cwd_together(cli):
    runner, group = cli
    assert runner.invoke(group, ["spawner", "--harness", "claude", "--yes"]).exit_code != 0


def test_the_spawner_command_saves_the_priority(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path), "--priority", "10", "--yes"])
    assert bridge.spawn_settings()["priority"] == 10


def test_the_bridge_publishes_its_spawn_priority(started, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.router, "list_all", lambda harness=None: [])
    bridge.save_spawn_settings({**bridge.spawn_settings(), "priority": 10})
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    try:
        bridge.Bridge(hubclient.HubClient(url, state.add_token("mac"), "mac"), "mac",
                      bridge.Ingress(tmp_path / "ingress.db")).publish()
        with sqlite3.connect(state.path) as db:
            assert db.execute("SELECT priority FROM spawners WHERE node = 'mac'").fetchone() == (10,)
    finally:
        stop()


def test_a_spawn_order_writes_the_workspace_guide(started, tmp_path):
    bridge.deliver_inbound(order())
    wait_for(started)
    assert (tmp_path / "AGENTS.md").read_text().startswith(workspace.MARKER)


def test_a_started_session_is_published_at_once(started):
    published = []
    bridge.deliver_inbound(order(), on_started=lambda: published.append(True))
    wait_for(published)
    assert published == [True]


def test_the_spawner_command_saves_the_env_file(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path),
                          "--env-file", str(tmp_path / "env.sh"), "--yes"])
    assert bridge.spawn_settings()["env_file"] == str((tmp_path / "env.sh").resolve())
