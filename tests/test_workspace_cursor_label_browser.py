"""On the phone, a Cursor session is labelled cursor-cli or cursor-ide, never just Cursor."""

import time

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired


def cursor_session(address, product):
    now = time.time()
    return {"address": address, "name": None, "status": "idle", "online": True, "cwd": "/workspace/app",
            "product": product, "presence": {"state": "idle", "label": "Idle", "detail": "Reported by the bridge.",
                                             "last_seen": now, "bridge_last_seen": now, "bridge_online": True,
                                             "can_message": True}}


@pytest.fixture
def workspace_ui(paired):
    page, _state = paired
    state = {"at": time.time(), "names": {}, "topics": [{"topic": "general", "members": ["user"]}],
             "sessions": [cursor_session("mac/cursor:cli00001", "cursor-cli"),
                          cursor_session("mac/cursor:ide00001", "cursor-ide")]}
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": {"pairing": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": []}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": []}))
    page.goto(page.url.split("#")[0] + "#topic=general", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text="general").wait_for()
    return page


def test_an_agent_cli_session_is_labelled_cursor_cli(workspace_ui):
    assert workspace_ui.locator("#people .agent-name").nth(0).inner_text() == "cursor-cli · cli00001"


def test_an_ide_session_is_labelled_cursor_ide(workspace_ui):
    assert workspace_ui.locator("#people .agent-name").nth(1).inner_text() == "cursor-ide · ide00001"


def test_an_agent_cli_avatar_reads_cl(workspace_ui):
    assert workspace_ui.locator("#people .avatar").nth(0).inner_text() == "CL"
