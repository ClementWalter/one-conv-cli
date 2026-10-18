"""A human request's owner on the phone: who has it, reassigning it, and the channel lead, at 320 and 390 px."""

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser

OVERFLOW = "() => document.documentElement.scrollWidth <= window.innerWidth"


@pytest.fixture
def workspace(real_http_workspace):
    """#release has @coordinator (the default lead, with a long session title) and @builder; @outsider is not in it."""
    worker = real_http_workspace["worker"]
    worker.put("/v1/sessions", json={"sessions": [
        {"address": "omp:lead", "name": "coordinator", "status": "idle", "title": "Coordinates every release " * 4},
        {"address": "omp:agent", "name": "builder", "status": "idle"},
        {"address": "omp:outsider", "name": "outsider", "status": "idle"}]}).raise_for_status()
    for address in ("omp:lead", "omp:agent"):
        worker.put(f"/v1/topics/release/members/{address}").raise_for_status()
    real_http_workspace["browser"].post(
        "/api/messages", json={"id": "a" * 32, "target": "#release", "text": "Ship the release notes today."},
        headers=real_http_workspace["headers"]).raise_for_status()
    return real_http_workspace


@pytest.fixture(params=[320, 390])
def phone(browser, workspace, request):
    context = browser.new_context(viewport={"width": request.param, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block")
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    # A full navigation to the channel, so no refresh left over from pairing can pick the view.
    page.goto("about:blank")
    page.goto(workspace["origin"] + "/#topic=release")
    page.wait_for_function("() => location.hash === '#topic=release' && typeof model !== 'undefined'"
                           " && model.view && model.view.kind === 'topic' && model.view.key === 'release'")
    page.locator(".task-chip").wait_for()
    yield page, workspace
    context.close()


def test_a_request_shows_the_lead_as_owner(phone):
    page, _ = phone
    assert page.locator(".task-chip .task-state").inner_text() == "Assigned to @coordinator"


def test_the_chip_says_when_the_request_last_changed(phone):
    page, _ = phone
    assert page.locator(".task-chip .task-updated").inner_text().startswith("updated ")


def test_the_request_chip_does_not_overflow(phone):
    page, _ = phone
    assert page.evaluate(OVERFLOW) is True


@pytest.fixture
def reassigned(phone):
    page, workspace = phone
    page.locator(".task-chip select").select_option(label="@builder")
    page.locator(".task-chip .task-state").filter(has_text="Assigned to @builder").wait_for(timeout=15000)
    return page, workspace


def test_reassigning_in_the_chip_changes_the_owner(reassigned):
    page, _ = reassigned
    assert page.locator(".task-chip .task-state").inner_text() == "Assigned to @builder"


def test_reassigning_in_the_chip_is_stored(reassigned):
    _, workspace = reassigned
    task = workspace["browser"].get("/api/topics/release").json()["messages"][0]["task"]
    assert task["owner"] == "worker/omp:agent"


def test_a_reassigned_chip_does_not_overflow(reassigned):
    page, _ = reassigned
    assert page.evaluate(OVERFLOW) is True


def test_reassigning_adds_no_message_the_human_did_not_write(reassigned):
    page, _ = reassigned
    page.wait_for_timeout(1500)  # The change feed has delivered the hub's notices, if it were to show them.
    assert page.locator("#messages .message-body").all_inner_texts() == ["Ship the release notes today."]


@pytest.fixture
def picking(phone):
    """The human has the reassign picker open while the owner reports progress."""
    page, workspace = phone
    task = workspace["browser"].get("/api/topics/release").json()["messages"][0]["task"]
    page.locator(".task-chip select").focus()
    workspace["worker"].post("/v1/tasks/update", json={"key": task["key"], "actor": "omp:lead",
                                                       "version": task["version"], "state": "working",
                                                       "note": "drafting the notes"}).raise_for_status()
    page.wait_for_timeout(3000)  # The change feed has brought the update to this page.
    return page


def test_a_progress_update_keeps_the_open_picker(picking):
    assert picking.evaluate("() => document.activeElement.classList.contains('task-assign')"
                            " && document.activeElement.isConnected") is True


def test_closing_the_picker_shows_the_current_progress(picking):
    picking.evaluate("() => document.activeElement.blur()")
    picking.locator(".task-chip .task-note").filter(has_text="drafting the notes").wait_for(timeout=5000)
    assert picking.locator(".task-chip .task-note").inner_text() == "drafting the notes"


@pytest.fixture
def stale(phone):
    """Someone else reassigns the request first; this phone still holds the old version."""
    page, workspace = phone
    task = workspace["browser"].get("/api/topics/release").json()["messages"][0]["task"]
    held = []
    page.route("**/api/changes*", lambda route: held.append(route))
    page.evaluate("() => model.feedAbort && model.feedAbort.abort()")
    workspace["browser"].post("/api/tasks/assign", json={"key": task["key"], "version": task["version"], "to": None},
                              headers=workspace["headers"]).raise_for_status()
    page.locator(".task-chip select").select_option(label="@builder")
    page.locator("#toast:not([hidden])").wait_for()
    return page


def test_a_stale_reassignment_shows_the_server_error(stale):
    assert "changed since you looked" in stale.locator("#toast").inner_text()


def test_a_stale_reassignment_refreshes_the_chip(stale):
    stale.locator(".task-chip .task-state").filter(has_text="Unassigned").wait_for(timeout=15000)
    assert stale.locator(".task-chip .task-state").inner_text() == "Unassigned"


@pytest.fixture
def details(phone):
    page, workspace = phone
    page.locator("#members-toggle").click()
    page.locator("#channel-lead .lead-name").wait_for()
    return page, workspace


def test_the_details_show_the_channel_lead(details):
    page, _ = details
    assert page.locator("#channel-lead .lead-name").inner_text() == "@coordinator · default"


def test_the_lead_picker_offers_only_channel_members(details):
    page, _ = details
    labels = page.locator("#channel-lead select option").all_inner_texts()
    assert labels == ["Default (@coordinator)", "@builder", "@coordinator"]


def test_the_lead_picker_starts_on_the_default(details):
    page, _ = details
    assert page.locator("#channel-lead select").input_value() == "__default"


def test_the_channel_lead_panel_does_not_overflow(details):
    page, _ = details
    assert page.evaluate(OVERFLOW) is True


@pytest.fixture
def lead_changed(details):
    page, workspace = details
    page.locator("#channel-lead select").select_option(label="@builder")
    page.locator("#channel-lead .lead-name").filter(has_text="@builder").wait_for(timeout=15000)
    return page, workspace


def test_changing_the_lead_updates_the_panel(lead_changed):
    page, _ = lead_changed
    assert page.locator("#channel-lead .lead-name").inner_text() == "@builder"


def test_changing_the_lead_is_stored(lead_changed):
    _, workspace = lead_changed
    topics = workspace["browser"].get("/api/state").json()["topics"]
    assert next(t for t in topics if t["topic"] == "release")["lead"]["lead"] == "worker/omp:agent"


@pytest.fixture
def outside_lead(phone):
    """The lead is set elsewhere to a connected agent who is not in #release."""
    page, workspace = phone
    workspace["browser"].post("/api/leads", json={"topic": "release", "lead": "@outsider"},
                              headers=workspace["headers"]).raise_for_status()
    page.locator("#members-toggle").click()
    warning = page.locator("#channel-lead .lead-warning")
    warning.wait_for(timeout=15000)
    return warning


def test_a_non_member_lead_is_flagged(outside_lead):
    assert outside_lead.inner_text().startswith("Not a member of #release")
