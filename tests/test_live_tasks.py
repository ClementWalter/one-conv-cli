"""Human requests as tasks: one owner is woken, the rest observe, and ownership is fenced by version."""

import sqlite3
import threading

import pytest

from one_conv.live import tasks
from one_conv.live.hub import Hub, HubError

HUMAN = "workspace"
LEAD = "mac/claude:lead"
PEER = "mac/codex:peer"
OTHER = "pc/omp:other"


def post(hub, ident, text="Book the train", to="#general", sender="user", node=HUMAN, reply="expected", **extra):
    return hub.send(node, {"id": ident * 32 if len(ident) == 1 else ident, "from": sender, "to": to, "text": text,
                           "reply": reply, "mode": "push", **extra}, join_sender=to.startswith("#") and node == HUMAN)


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"), general="general")
    state.publish("mac", [{"address": "claude:lead", "name": "coordinator"}, {"address": "codex:peer", "name": "peer"}])
    state.publish("pc", [{"address": "omp:other", "name": "other"}])
    state.publish(HUMAN, [{"address": "user"}])
    state.adopt_trusted_human(HUMAN)
    return state


@pytest.fixture
def request_task(hub):
    return post(hub, "a")["task"]


def deliveries(hub, task_key=None):
    with sqlite3.connect(hub.path) as db:
        return db.execute("SELECT d.to_node || '/' || d.to_address, d.role, d.mode FROM deliveries d JOIN posts p "
                          "ON p.seq = d.post WHERE d.task IS ? OR ? IS NULL ORDER BY d.rowid",
                          (task_key, task_key)).fetchall()


def task_count(hub):
    with sqlite3.connect(hub.path) as db:
        return db.execute("SELECT COUNT(*) FROM tasks WHERE parent IS NULL").fetchone()[0]


def test_a_human_channel_request_is_owned_by_the_channel_lead(request_task):
    assert request_task["owner"] == LEAD


@pytest.mark.parametrize("route, role, mode", [(LEAD, "owner", "push"), (PEER, "observer", "fyi"),
                                                (OTHER, "observer", "fyi")])
def test_only_the_owner_is_woken_by_a_channel_request(hub, request_task, route, role, mode):
    assert (route, role, mode) in deliveries(hub, request_task["key"])


def test_a_retried_request_finds_the_same_task(hub, request_task):
    assert post(hub, "a")["task"]["key"] == request_task["key"]


def test_a_retried_request_creates_no_second_task(hub, request_task):
    post(hub, "a")
    assert task_count(hub) == 1


def test_an_agent_post_creates_no_task(hub):
    post(hub, "b", sender="claude:lead", node="mac")
    assert task_count(hub) == 0


def test_an_announcement_creates_no_task(hub):
    post(hub, "b", reply="none")
    assert task_count(hub) == 0


def test_an_untrusted_node_user_creates_no_task(hub):
    post(hub, "b", node="mac")
    assert task_count(hub) == 0


def test_a_channel_without_an_available_lead_is_visibly_unassigned(hub):
    hub.set_lead("general", "@nobody", f"{HUMAN}/user")
    assert post(hub, "b")["task"]["state"] == "unassigned"


def test_an_unassigned_request_wakes_nobody(hub):
    hub.set_lead("general", "@nobody", f"{HUMAN}/user")
    key = post(hub, "b")["task"]["key"]
    assert {mode for _, _, mode in deliveries(hub, key)} == {"fyi"}


def test_a_human_direct_message_is_owned_by_its_recipient(hub):
    assert post(hub, "b", to=PEER)["task"]["owner"] == PEER


def test_two_simultaneous_claims_leave_one_owner(hub):
    hub.set_lead("general", "@nobody", f"{HUMAN}/user")
    key = post(hub, "b")["task"]["key"]
    winners = []

    def race(node, address):
        try:
            winners.append(hub.claim(node, {"key": key, "actor": address})["owner"])
        except HubError:
            pass
    threads = [threading.Thread(target=race, args=pair) for pair in
               [("mac", "codex:peer"), ("pc", "omp:other"), ("mac", "claude:lead")]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1


def test_a_claim_retried_by_the_owner_keeps_the_version(hub, request_task):
    claimed = hub.claim("mac", {"key": request_task["key"], "actor": "claude:lead"})
    assert claimed["version"] == request_task["version"]


def test_an_observer_cannot_claim_an_owned_task(hub, request_task):
    with pytest.raises(HubError):
        hub.claim("mac", {"key": request_task["key"], "actor": "codex:peer"})


def test_a_refused_claim_carries_the_current_owner(hub, request_task):
    with pytest.raises(HubError) as refused:
        hub.claim("mac", {"key": request_task["key"], "actor": "codex:peer"})
    assert refused.value.task["owner"] == LEAD


def test_the_owner_reports_progress(hub, request_task):
    updated = hub.update_task("mac", {"key": request_task["key"], "actor": "claude:lead",
                                      "version": request_task["version"], "state": "working", "note": "on it"})
    assert updated["state"] == "working"


def test_an_observer_cannot_update_the_task(hub, request_task):
    with pytest.raises(HubError):
        hub.update_task("mac", {"key": request_task["key"], "actor": "codex:peer",
                                "version": request_task["version"], "state": "done"})


@pytest.fixture
def reassigned(hub, request_task):
    return hub.assign(request_task["key"], "@peer", f"{HUMAN}/user")


def test_reassigning_changes_the_owner(reassigned):
    assert reassigned["owner"] == PEER


def test_reassigning_creates_no_second_task(hub, reassigned):
    assert task_count(hub) == 1


def test_the_former_owner_is_refused_after_reassignment(hub, request_task, reassigned):
    with pytest.raises(HubError):
        hub.update_task("mac", {"key": request_task["key"], "actor": "claude:lead",
                                "version": request_task["version"], "state": "done"})


def test_reassignment_wakes_the_new_owner_who_had_only_observed(hub, reassigned):
    assert (PEER, "owner", "push") in deliveries(hub, reassigned["key"])


def test_reassignment_tells_the_former_owner_to_stop(hub, reassigned):
    assert (LEAD, "observer", "fyi") in deliveries(hub, reassigned["key"])


def test_the_new_owner_acts_with_the_new_version(hub, reassigned):
    updated = hub.update_task("mac", {"key": reassigned["key"], "actor": "codex:peer",
                                      "version": reassigned["version"], "state": "working"})
    assert updated["owner"] == PEER


@pytest.fixture
def child(hub, request_task):
    return hub.delegate("mac", {"key": request_task["key"], "actor": "claude:lead",
                                "version": request_task["version"], "to": "@other", "kind": "review",
                                "note": "review the plan"})


def test_a_child_task_belongs_to_the_collaborator(child):
    assert child["owner"] == OTHER


def test_a_child_task_wakes_its_collaborator(hub, child):
    assert (OTHER, "collaborator", "push") in deliveries(hub, child["key"])


def test_a_child_task_leaves_the_request_with_its_owner(hub, request_task, child):
    assert hub.task(request_task["key"])["owner"] == LEAD


def test_a_retried_grant_returns_the_same_child(hub, request_task, child):
    again = hub.delegate("mac", {"key": request_task["key"], "actor": "claude:lead",
                                 "version": request_task["version"], "to": "@other", "kind": "review",
                                 "note": "review the plan"})
    assert again["key"] == child["key"]


def test_a_collaborator_cannot_update_the_parent_request(hub, request_task, child):
    with pytest.raises(HubError):
        hub.update_task("pc", {"key": request_task["key"], "actor": "omp:other",
                               "version": request_task["version"], "state": "done"})


def test_a_request_with_an_open_child_cannot_be_done(hub, request_task, child):
    with pytest.raises(HubError):
        hub.update_task("mac", {"key": request_task["key"], "actor": "claude:lead",
                                "version": request_task["version"], "state": "done"})


def test_an_observer_reply_linked_to_the_task_is_refused(hub, request_task):
    with pytest.raises(HubError):
        post(hub, "c", to="#general", sender="codex:peer", node="mac",
             task={"key": request_task["key"], "version": request_task["version"]})


def test_the_owner_reply_linked_to_the_task_is_accepted(hub, request_task):
    receipt = post(hub, "c", to="#general", sender="claude:lead", node="mac",
                   task={"key": request_task["key"], "version": request_task["version"]})
    assert receipt["duplicate"] is False


def test_a_former_owner_reply_linked_to_the_task_is_refused(hub, request_task, reassigned):
    with pytest.raises(HubError):
        post(hub, "c", to="#general", sender="claude:lead", node="mac",
             task={"key": request_task["key"], "version": request_task["version"]})


def test_replaying_history_creates_no_work(hub, request_task):
    hub.history("general")
    assert task_count(hub) == 1


def test_history_names_the_task_a_request_made(hub, request_task):
    assert hub.history("general")[0]["task"] == request_task["key"]


@pytest.fixture
def old_hub(tmp_path):
    """A hub with a human broadcast queued before tasks existed."""
    path = str(tmp_path / "old.db")
    state = Hub(path, general="general")
    state.publish("mac", [{"address": "claude:lead", "name": "coordinator"}, {"address": "codex:peer"}])
    state.publish(HUMAN, [{"address": "user"}])
    post(state, "d")
    upgraded = Hub(path, general="general")
    upgraded.adopt_trusted_human(HUMAN)
    return upgraded


def test_an_old_queued_human_broadcast_becomes_context_after_upgrade(old_hub):
    assert {mode for _, _, mode in deliveries(old_hub)} == {"fyi"}


def test_an_old_queued_human_broadcast_gets_no_task_after_upgrade(old_hub):
    assert task_count(old_hub) == 0


def test_tasks_reject_a_malformed_key():
    with pytest.raises(tasks.TaskError):
        tasks.check_key("not a key")
