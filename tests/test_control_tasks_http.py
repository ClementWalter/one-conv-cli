"""Channel requests are offered to running members, and the paired human assigns them, over real HTTPS."""

import pytest

from tests import test_control_messages as https_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace

REQUEST_ID = "a" * 32
LEAD = "worker/omp:lead"
BUILDER = "worker/omp:agent"


@pytest.fixture
def workspace(real_http_workspace):
    """Connected agents: @coordinator and @builder in #release, @outsider outside it."""
    worker = real_http_workspace["worker"]
    worker.put("/v1/sessions", json={"sessions": [
        {"address": "omp:lead", "name": "coordinator", "status": "idle"},
        {"address": "omp:agent", "name": "builder", "status": "idle"},
        {"address": "omp:outsider", "name": "outsider", "status": "idle"}]}).raise_for_status()
    for address in ("omp:lead", "omp:agent"):
        worker.put(f"/v1/topics/release/members/{address}").raise_for_status()
    return real_http_workspace


@pytest.fixture
def request_task(workspace):
    """The task the human's request in #release created."""
    browser, headers = workspace["browser"], workspace["headers"]
    browser.post("/api/messages", json={"id": REQUEST_ID, "target": "#release", "text": "Ship the release."},
                 headers=headers).raise_for_status()
    return browser.get("/api/topics/release").json()["messages"][-1]["task"]


def assign(workspace, body, headers=None):
    return workspace["browser"].post("/api/tasks/assign", json=body,
                                     headers=workspace["headers"] if headers is None else headers)


def shown_task(workspace):
    return workspace["browser"].get("/api/topics/release").json()["messages"][0]["task"]


def test_a_channel_request_starts_unclaimed(request_task):
    assert request_task["owner"] is None


def test_a_channel_request_is_offered_to_its_running_members(workspace, request_task):
    inbox = workspace["worker"].get("/v1/inbox").json()["messages"]
    assert sorted(item["to"] for item in inbox if item.get("role") == "candidate") == ["omp:agent", "omp:lead"]


def test_a_channel_request_is_not_offered_outside_the_channel(workspace, request_task):
    inbox = workspace["worker"].get("/v1/inbox").json()["messages"]
    assert "omp:outsider" not in {item["to"] for item in inbox}


def test_the_first_claim_owns_the_request(workspace, request_task):
    workspace["worker"].post("/v1/tasks/claim", json={"key": request_task["key"], "actor": "omp:agent",
                                                     "version": request_task["version"]}).raise_for_status()
    assert shown_task(workspace)["owner"] == BUILDER


def channel_state(workspace, topic="release"):
    topics = workspace["browser"].get("/api/state").json()["topics"]
    return next(item for item in topics if item["topic"] == topic)


def test_the_channel_counts_its_running_members(workspace):
    assert channel_state(workspace)["running"] == 2


def test_the_channel_lists_who_took_part(workspace):
    workspace["worker"].post("/v1/messages", json={"id": "d" * 32, "from": "omp:agent", "to": "#release",
                                                  "text": "On it."}).raise_for_status()
    assert channel_state(workspace)["participants"] == [BUILDER]


def test_the_workspace_has_no_lead_route(workspace):
    response = workspace["browser"].post("/api/leads", json={"topic": "release", "lead": "@builder"},
                                         headers=workspace["headers"])
    assert response.status_code in (404, 405)


def test_the_session_advertises_tasks(workspace):
    assert workspace["browser"].get("/api/session").json()["capabilities"]["tasks"] is True


@pytest.mark.parametrize("drop", ["Origin", "X-One-Conv-Token"])
def test_assign_requires_origin_and_csrf(workspace, request_task, drop):
    headers = {key: value for key, value in workspace["headers"].items() if key != drop}
    response = assign(workspace, {"key": request_task["key"], "version": request_task["version"], "to": BUILDER},
                      headers)
    assert response.status_code == 403


@pytest.fixture
def reassigned(workspace, request_task):
    response = assign(workspace, {"key": request_task["key"], "version": request_task["version"], "to": BUILDER})
    response.raise_for_status()
    return workspace, request_task, response.json()


def test_assign_returns_the_new_owner(reassigned):
    assert reassigned[2]["task"]["owner"] == BUILDER


def test_assign_changes_the_owner_shown_in_the_channel(reassigned):
    assert shown_task(reassigned[0])["owner"] == BUILDER


def test_assign_moves_the_version_forward(reassigned):
    _, before, _ = reassigned
    assert shown_task(reassigned[0])["version"] == before["version"] + 1


def test_the_channel_shows_no_message_the_human_did_not_write(reassigned):
    workspace = reassigned[0]
    assert [m["text"] for m in workspace["browser"].get("/api/topics/release").json()["messages"]] == \
        ["Ship the release."]


def test_direct_threads_show_no_assignment_notice(reassigned):
    workspace = reassigned[0]
    threads = workspace["browser"].get("/api/direct").json()["threads"]
    assert [m["text"] for thread in threads for m in thread["messages"]] == []


def test_the_topic_history_agents_read_has_no_assignment_notice(reassigned):
    workspace = reassigned[0]
    history = workspace["worker"].get("/v1/topics/release/messages").json()["messages"]
    assert [row["text"] for row in history] == ["Ship the release."]


def test_the_new_owner_receives_the_assignment_marked_as_from_the_hub(reassigned):
    workspace = reassigned[0]
    inbox = workspace["worker"].get("/v1/inbox").json()["messages"]
    assert [item.get("system", False) for item in inbox if item["to"] == "omp:agent" and item.get("role") == "owner"] \
        == [True]


def test_an_agent_cannot_hide_its_post_by_calling_it_system(workspace):
    workspace["worker"].post("/v1/messages", json={"id": "c" * 32, "from": "omp:agent", "to": "#release",
                                                  "text": "visible", "kind": "system"}).raise_for_status()
    history = workspace["worker"].get("/v1/topics/release/messages").json()["messages"]
    assert [row["text"] for row in history] == ["visible"]


def test_a_stale_version_is_refused(reassigned):
    workspace, before, _ = reassigned
    response = assign(workspace, {"key": before["key"], "version": before["version"], "to": LEAD})
    assert response.status_code == 409


def test_a_stale_version_keeps_the_newer_owner(reassigned):
    workspace, before, _ = reassigned
    assign(workspace, {"key": before["key"], "version": before["version"], "to": LEAD})
    assert shown_task(workspace)["owner"] == BUILDER


@pytest.fixture
def unassigned(workspace, request_task):
    assign(workspace, {"key": request_task["key"], "version": request_task["version"], "to": None}).raise_for_status()
    return shown_task(workspace)


def test_unassign_clears_the_owner(unassigned):
    assert unassigned["owner"] is None


def test_unassign_marks_the_request_unassigned(unassigned):
    assert unassigned["state"] == "unassigned"


@pytest.mark.parametrize("body", [
    {"version": 1, "to": BUILDER},
    {"key": "release", "version": 1, "to": BUILDER},
    {"key": "workspace:" + REQUEST_ID, "to": BUILDER},
    {"key": "workspace:" + REQUEST_ID, "version": 1, "to": 7},
    {"key": "workspace:" + REQUEST_ID, "version": 1, "to": BUILDER, "owner": BUILDER},
], ids=["no-key", "bad-key", "no-version", "bad-to", "extra-field"])
def test_an_invalid_assignment_is_refused(workspace, request_task, body):
    assert assign(workspace, body).status_code == 400


def test_assigning_to_an_unpublished_agent_is_refused(workspace, request_task):
    response = assign(workspace, {"key": request_task["key"], "version": request_task["version"],
                                  "to": "worker/omp:missing"})
    assert response.status_code == 404
