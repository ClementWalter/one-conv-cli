"""The paired human assigns requests and chooses channel leads over real HTTPS."""

import pytest

from tests import test_control_messages as https_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace

REQUEST_ID = "a" * 32
LEAD = "worker/omp:lead"
BUILDER = "worker/omp:agent"


@pytest.fixture
def workspace(real_http_workspace):
    """Connected agents: @coordinator (the default lead) and @builder in #release, @outsider outside it."""
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


def test_a_request_is_owned_by_the_default_lead(request_task):
    assert request_task["owner"] == LEAD


def test_the_session_advertises_tasks(workspace):
    assert workspace["browser"].get("/api/session").json()["capabilities"]["tasks"] is True


@pytest.mark.parametrize("drop", ["Origin", "X-One-Conv-Token"])
def test_assign_requires_origin_and_csrf(workspace, request_task, drop):
    headers = {key: value for key, value in workspace["headers"].items() if key != drop}
    response = assign(workspace, {"key": request_task["key"], "version": request_task["version"], "to": BUILDER},
                      headers)
    assert response.status_code == 403


@pytest.mark.parametrize("drop", ["Origin", "X-One-Conv-Token"])
def test_lead_change_requires_origin_and_csrf(workspace, drop):
    headers = {key: value for key, value in workspace["headers"].items() if key != drop}
    response = workspace["browser"].post("/api/leads", json={"topic": "release", "lead": "@builder"}, headers=headers)
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


def lead_of(workspace, topic="release"):
    topics = workspace["browser"].get("/api/state").json()["topics"]
    return next(item for item in topics if item["topic"] == topic)["lead"]


def test_the_default_lead_is_reported(workspace):
    assert lead_of(workspace)["lead"] == "@coordinator"


@pytest.fixture
def lead_changed(workspace):
    response = workspace["browser"].post("/api/leads", json={"topic": "release", "lead": "@builder"},
                                         headers=workspace["headers"])
    response.raise_for_status()
    return workspace, response.json()


def test_a_lead_change_answers_with_the_resolved_route(lead_changed):
    assert lead_changed[1]["route"] == BUILDER


def test_a_lead_change_is_reflected_in_state(lead_changed):
    assert lead_of(lead_changed[0])["lead"] == "@builder"


def test_a_lead_change_owns_the_next_request(lead_changed):
    workspace, _ = lead_changed
    workspace["browser"].post("/api/messages", json={"id": "c" * 32, "target": "#release", "text": "Next one."},
                              headers=workspace["headers"]).raise_for_status()
    assert workspace["browser"].get("/api/topics/release").json()["messages"][-1]["task"]["owner"] == BUILDER


def test_resetting_the_lead_restores_the_default(lead_changed):
    workspace, _ = lead_changed
    workspace["browser"].post("/api/leads", json={"topic": "release", "lead": None},
                              headers=workspace["headers"]).raise_for_status()
    assert lead_of(workspace)["lead"] == "@coordinator"


@pytest.mark.parametrize("body", [
    {"topic": "release"},
    {"topic": "release", "lead": 3},
    {"topic": "release", "lead": "builder"},
    {"topic": "Not A Topic", "lead": "@builder"},
    {"topic": "release", "lead": "@builder", "extra": True},
], ids=["no-lead", "bad-type", "bad-lead", "bad-topic", "extra-field"])
def test_an_invalid_lead_is_refused(workspace, body):
    response = workspace["browser"].post("/api/leads", json=body, headers=workspace["headers"])
    assert response.status_code == 400


@pytest.fixture
def outside_lead(workspace):
    workspace["browser"].post("/api/leads", json={"topic": "release", "lead": "@outsider"},
                              headers=workspace["headers"]).raise_for_status()
    return lead_of(workspace)


def test_a_non_member_lead_is_not_available(outside_lead):
    assert outside_lead["available"] is False


def test_a_non_member_lead_is_reported_as_not_a_member(outside_lead):
    assert outside_lead["member"] is False


def test_a_member_lead_is_reported_as_a_member(lead_changed):
    assert lead_of(lead_changed[0])["member"] is True
