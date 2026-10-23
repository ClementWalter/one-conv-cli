"""On the phone, a running session without an @name is listed under its own session name."""

import time

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired


def claude_session(address, name, title):
    now = time.time()
    return {"address": address, "name": name, "title": title, "status": "idle", "online": True, "cwd": "/work/app",
            "presence": {"state": "idle", "label": "Idle", "detail": "Reported by the bridge.", "last_seen": now,
                         "bridge_last_seen": now, "bridge_online": True, "can_message": True}}


@pytest.fixture
def workspace_ui(paired):
    """claude:aaa is called one-conv-cli-df in Claude Code; claude:bbb also has the @name lead."""
    page, _state = paired
    state = {"at": time.time(), "names": {}, "topics": [{"topic": "general", "members": ["user"]}],
             "sessions": [claude_session("mac/claude:aaa00001", None, "one-conv-cli-df"),
                          claude_session("mac/claude:bbb00001", "lead", "one-conv-cli-8f")]}
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": {"pairing": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": []}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": []}))
    page.goto(page.url.split("#")[0] + "#topic=general", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text="general").wait_for()
    return page


def test_a_session_is_listed_under_its_own_name(workspace_ui):
    assert workspace_ui.locator("#people .agent-name").nth(0).inner_text() == "one-conv-cli-df"


def test_the_harness_and_short_id_move_to_the_context_line(workspace_ui):
    assert workspace_ui.locator("#people .agent-context").nth(0).inner_text() == "Claude · aaa00001 · mac · app"


def test_an_at_name_still_comes_first(workspace_ui):
    assert workspace_ui.locator("#people .agent-name").nth(1).inner_text() == "@lead"


def test_the_avatar_names_the_harness(workspace_ui):
    assert workspace_ui.locator("#people .avatar").nth(0).inner_text() == "CC"


def test_a_renamed_agent_avatar_still_names_the_harness(workspace_ui):
    assert workspace_ui.locator("#people .avatar").nth(1).inner_text() == "CC"
