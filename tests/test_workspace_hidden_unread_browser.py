"""On the phone, every unread counted in the title can be found: an ended agent's unread DM stays listed."""

import time

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired

GONE = "worker-01/claude:gone000001"
VANISHED = "macbook/claude:vanished01"
QUIET = "macbook/claude:quiet00001"


def offline(address, name):
    return {"address": address, "name": name, "status": "", "online": False, "cwd": "/workspace/project",
            "presence": {"state": "offline", "label": "Offline", "detail": "Not published.",
                         "last_seen": None, "bridge_last_seen": None, "bridge_online": False, "can_message": False}}


@pytest.fixture
def workspace_ui(paired):
    """@gone ended with 3 unread DMs, @vanished left the directory with 1, @quiet ended with none; #old is archived."""
    page, _state = paired
    state = {"at": time.time(), "sessions": [offline(GONE, "gone"), offline(QUIET, "quiet")], "names": {}, "topics": [
        {"topic": "live", "members": ["user"], "unread": 0},
        {"topic": "old", "members": ["user"], "unread": 5, "archived": True}]}
    threads = [{"pair": "user " + GONE, "unread": 3, "messages": []},
               {"pair": VANISHED + " user", "unread": 1, "messages": []},
               {"pair": QUIET + " user", "unread": 0, "messages": []}]
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": {"pairing": True, "unread": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": threads}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": []}))
    page.goto(page.url.split("#")[0] + "#topic=live", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text="live").wait_for()
    return page


def test_an_ended_agent_with_unread_messages_is_listed(workspace_ui):
    assert workspace_ui.locator("#people button.unread").filter(has_text="@gone").count() == 1


def test_an_agent_gone_from_the_directory_with_unread_messages_is_listed(workspace_ui):
    assert workspace_ui.locator("#people button.unread").filter(has_text="vanished").count() == 1


def test_an_ended_agent_with_nothing_unread_is_not_listed(workspace_ui):
    assert workspace_ui.locator("#people button").filter(has_text="@quiet").count() == 0


def test_an_archived_channel_does_not_add_to_the_title(workspace_ui):
    assert workspace_ui.title().startswith("(4) ") is True
