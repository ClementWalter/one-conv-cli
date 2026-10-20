"""Stable phone navigation and evidence-based presence use production UI assets."""

import time
from urllib.parse import urlsplit

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired


@pytest.fixture
def workspace_ui(paired):
    """Explicit reports separate native context, identity, and hub delivery evidence."""
    page, _state = paired
    now = time.time()
    sessions = [
        {"address": "laptop/codex:1234567890", "name": "jack", "status": "loaded", "online": True,
         "title": "A changing native transcript title", "cwd": "/workspace/project", "presence": {
             "state": "unverified", "label": "Unverified", "detail": "A message may be queued.",
             "last_seen": now, "bridge_last_seen": now, "bridge_online": True, "can_message": True}},
        {"address": "laptop/claude:abcdefghij", "status": "busy", "online": True, "cwd": "/workspace/review",
         "presence": {"state": "busy", "label": "Busy", "detail": "Model access is not verified.",
                      "last_seen": now, "bridge_last_seen": now, "bridge_online": True, "can_message": True}},
        {"address": "old/claude:old-session", "title": "Old conversation", "history_only": True,
         "presence": {"state": "offline", "label": "Offline", "detail": "No longer published.",
                      "last_seen": None, "bridge_last_seen": None, "bridge_online": False, "can_message": False}},
    ]
    state = {"at": now, "sessions": sessions, "topics": [{"topic": "alpha", "members": []},
                                             {"topic": "zulu", "members": [sessions[0]["address"]]}], "names": {}}
    messages = [{"id": "a" * 32, "from": "user", "to": sessions[0]["address"], "text": "Please check this.",
                 "at": now - 30, "delivery": {"state": "accepted", "label": "Accepted by adapter", "detail": "Not a read receipt."}}]
    threads = [{"pair": "laptop/codex:1234567890 user", "messages": messages},
               {"pair": "old/claude:old-session user", "messages": []}]
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": {"pairing": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": threads}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": []}))
    page.goto(page.url.split("#")[0] + "#topic=zulu", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text="zulu").wait_for()
    page.locator("#people .agent-name").filter(has_text="@jack").wait_for()
    return page, state, threads


def test_root_launch_restores_the_selected_channel(workspace_ui):
    page = workspace_ui[0]
    page.goto(page.url.split("#")[0], wait_until="networkidle")
    page.reload(wait_until="networkidle")
    assert page.locator("#title").inner_text() == "zulu"


def test_missing_channel_route_is_not_replaced_alphabetically(workspace_ui):
    page = workspace_ui[0]
    page.goto(page.url.split("#")[0] + "#topic=missing-history", wait_until="networkidle")
    assert page.locator("#title").inner_text() == "missing-history"


def test_explicit_channel_route_overrides_saved_selection(workspace_ui):
    page = workspace_ui[0]
    page.goto(page.url.split("#")[0] + "#topic=alpha", wait_until="networkidle")
    assert page.locator("#title").inner_text() == "alpha"


def test_home_rail_preserves_the_last_channel(workspace_ui):
    page = workspace_ui[0]
    page.set_viewport_size({"width": 1280, "height": 900})
    page.locator("#nav-activity").click()
    page.locator("#nav-home").click()
    assert page.locator("#title").inner_text() == "zulu"


def test_hash_navigation_uses_the_exact_channel_key(workspace_ui):
    page = workspace_ui[0]
    page.evaluate("location.hash = 'topic=alpha'")
    page.locator("#title").filter(has_text="alpha").wait_for()
    assert page.locator("#title").inner_text() == "alpha"


def test_loaded_thread_is_not_a_green_connection(workspace_ui):
    assert workspace_ui[0].locator("#people .conversation-row").filter(has_text="@jack").locator(".row-presence").get_attribute("class") == "row-presence unverified"


def test_unnamed_agents_have_stable_short_identity(workspace_ui):
    assert workspace_ui[0].locator("#people .agent-name").all_text_contents() == ["@jack", "Claude · abcdefgh"]


def test_sidebar_exposes_machine_and_project(workspace_ui):
    assert "Codex · laptop · project" in workspace_ui[0].locator("#people").inner_text()


def test_sidebar_exposes_report_freshness(workspace_ui):
    assert "Connection unverified · " in workspace_ui[0].locator("#people").inner_text()


def test_sidebar_context_does_not_replace_identity(workspace_ui):
    assert workspace_ui[0].locator("#people .agent-session-context").inner_text() == "Context: A changing native transcript title"


def test_offline_history_is_separate_from_current_reports(workspace_ui):
    assert workspace_ui[0].locator("#people .agent-name").all_text_contents() == ["@jack", "Claude · abcdefgh"]


@pytest.fixture
def direct_ui(workspace_ui):
    page = workspace_ui[0]
    page.locator("#sidebar-toggle").click()
    page.locator("#people .conversation-row").filter(has_text="@jack").click()
    page.locator(".sidebar").evaluate("node => Promise.all(node.getAnimations().map(animation => animation.finished))")
    page.locator(".message-delivery").wait_for()
    return workspace_ui


def test_loaded_unverified_agent_can_still_receive_a_message(direct_ui):
    page = direct_ui[0]
    page.locator("#message-input").fill("A message may queue.")
    assert page.locator("#send-message").is_enabled()


def test_native_title_does_not_become_agent_identity(direct_ui):
    assert direct_ui[0].locator("#title").inner_text() == "@jack"


def test_direct_route_survives_root_launch(direct_ui):
    page = direct_ui[0]
    page.goto(page.url.split("#")[0], wait_until="networkidle")
    page.reload(wait_until="networkidle")
    assert urlsplit(page.url).fragment == "direct=laptop%2Fcodex%3A1234567890+user"


def test_bridge_acceptance_is_not_a_read_receipt(direct_ui):
    assert direct_ui[0].locator(".message-delivery").inner_text() == "Delivery accepted"


@pytest.mark.parametrize(("state", "label"), [("stored", "Saved · awaiting delivery"), ("refused", "Refused"),
                                             ("unconfirmed", "Unconfirmed")])
def test_bubble_delivery_uses_only_reported_evidence(direct_ui, state, label):
    page, _state, threads = direct_ui
    threads[0]["messages"][0]["delivery"]["state"] = state
    page.reload(wait_until="networkidle")
    assert page.locator(".message-delivery").inner_text() == label


def test_unanswered_request_has_one_conversation_status(direct_ui):
    assert direct_ui[0].locator("#conversation-response").inner_text() == "No later message from this agent yet."


def test_unrelated_agent_message_does_not_count_as_a_return(direct_ui):
    page, _state, threads = direct_ui
    threads[0]["messages"].append({"id": "b" * 32, "from": "laptop/claude:abcdefghij", "to": "user",
                                   "text": "An unrelated message.", "at": time.time()})
    page.reload(wait_until="networkidle")
    assert page.locator("#conversation-response").inner_text() == "No later message from this agent yet."


def test_later_return_is_activity_without_claiming_causal_reply(direct_ui):
    page, _state, threads = direct_ui
    threads[0]["messages"].append({"id": "b" * 32, "from": "laptop/codex:1234567890", "to": "user",
                                   "text": "An agent message.", "at": time.time()})
    page.reload(wait_until="networkidle")
    assert "Latest message from @jack" in page.locator("#conversation-response").inner_text()


def test_details_show_context_without_inventing_a_task(direct_ui):
    page = direct_ui[0]
    page.locator("#members-toggle").click()
    assert "Session context: A changing native transcript title" in page.locator("#details-content").inner_text()


def test_no_task_is_inferred_from_busy_status(workspace_ui):
    page = workspace_ui[0]
    page.evaluate("select('direct', 'laptop/claude:abcdefghij user')")
    page.locator("#members-toggle").click()
    assert "Current task is not reported." in page.locator("#details-content").inner_text()


def test_cached_connection_report_expires_when_polling_fails(workspace_ui):
    page = workspace_ui[0]
    page.clock.install()
    page.route("**/api/state", lambda route: route.abort())
    page.clock.fast_forward(65000)
    page.locator("#notice").filter(has_text="reconnecting").wait_for()
    page.locator("#people .row-presence.busy").wait_for(state="detached")
    assert page.locator("#people .row-presence.busy").count() == 0


@pytest.fixture
def invitation_ui(workspace_ui):
    page = workspace_ui[0]
    calls = []

    def invite(route):
        calls.append(route.request.post_data_json)
        route.fulfill(json={"url": page.url.split("#")[0] + "#pair=private-invite-token", "expires_at": time.time() + 600,
                            "single_use": True, "access": "owner"})

    page.route("**/api/devices/pair", invite)
    page.locator("#device-menu").click()
    return page, calls


def test_opening_settings_does_not_issue_an_invitation(invitation_ui):
    assert invitation_ui[1] == []


def test_invitation_discloses_full_owner_access(invitation_ui):
    assert "full owner access" in invitation_ui[0].locator(".device-connect").inner_text()


@pytest.fixture
def generated_invitation(invitation_ui):
    page = invitation_ui[0]
    page.locator("#generate-invitation").click()
    page.locator("#device-invitation").wait_for()
    return invitation_ui


def test_explicit_invitation_request_has_no_chosen_origin(generated_invitation):
    assert generated_invitation[1] == [{}]


def test_invitation_displays_one_use_and_expiry(generated_invitation):
    assert "One use · expires" in generated_invitation[0].locator("#invitation-expiry").inner_text()


def test_invitation_is_cleared_when_settings_close(generated_invitation):
    page = generated_invitation[0]
    page.get_by_role("button", name="Close device settings").click()
    assert page.locator("#device-invitation-url").input_value() == ""


def test_invitation_is_cleared_when_settings_are_dismissed_with_escape(generated_invitation):
    page = generated_invitation[0]
    page.keyboard.press("Escape")
    assert page.locator("#device-invitation-url").input_value() == ""


def test_invitation_is_not_persisted_in_browser_storage(generated_invitation):
    assert "private-invite-token" not in generated_invitation[0].evaluate("JSON.stringify([localStorage, sessionStorage])")


def test_invitation_is_not_written_to_navigation(generated_invitation):
    assert "private-invite-token" not in generated_invitation[0].url


def test_a_late_invitation_response_does_not_restore_the_closed_secret(invitation_ui):
    page = invitation_ui[0]
    pending = []
    page.route("**/api/devices/pair", lambda route: pending.append(route))
    with page.expect_request("**/api/devices/pair"):
        page.locator("#generate-invitation").click()
    page.locator("[data-close=device-dialog]").click()
    with page.expect_response("**/api/devices/pair"):
        pending[0].fulfill(json={"url": page.url.split("#")[0] + "#pair=late-secret", "expires_at": time.time() + 600})
    page.locator("#device-menu").click()
    assert page.locator("#device-invitation-url").input_value() == ""


def test_copy_invitation_has_a_manual_fallback(generated_invitation):
    page = generated_invitation[0]
    page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.reject(new Error('Denied'))}, configurable: true})")
    page.locator("#copy-invitation").click()
    page.locator("#invitation-feedback").filter(has_text="Select and copy").wait_for()
    assert page.locator("#device-invitation-url").evaluate("node => node.selectionEnd-node.selectionStart") > 0


def test_expired_invitation_is_not_copied(generated_invitation):
    page = generated_invitation[0]
    page.evaluate("deviceInvitation.expires_at = reportTime() - 1")
    page.locator("#copy-invitation").click()
    assert page.locator("#device-invitation-url").input_value() == ""


def test_replacing_an_invitation_hides_the_superseded_secret_on_failure(generated_invitation):
    page = generated_invitation[0]
    page.route("**/api/devices/pair", lambda route: route.fulfill(status=503, json={"error": "Temporarily unavailable"}))
    page.locator("#generate-invitation").click()
    page.locator("#invitation-feedback").filter(has_text="Temporarily unavailable").wait_for()
    assert page.locator("#device-invitation-url").input_value() == ""


def test_phone_presence_layout_has_no_horizontal_overflow(direct_ui):
    assert direct_ui[0].evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_phone_presence_screenshot(direct_ui, tmp_path):
    page = direct_ui[0]
    page.locator("#members-toggle").click()
    page.screenshot(path=str(tmp_path / "iphone-agent-presence.png"), full_page=True)
    assert page.locator("#details-content").is_visible()


def test_phone_connections_screenshot(workspace_ui, tmp_path):
    page = workspace_ui[0]
    page.locator("#sidebar-toggle").click()
    page.locator(".sidebar").evaluate("node => Promise.all(node.getAnimations().map(animation => animation.finished))")
    page.screenshot(path=str(tmp_path / "iphone-agent-connections.png"), full_page=True)
    assert page.locator("#people").is_visible()
