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
"""On the phone, a channel or DM shows which of its agents the bridge reports busy, above the composer."""

import time

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired


def agent(address, name, status):
    now = time.time()
    return {"address": address, "name": name, "status": status, "online": True, "cwd": "/workspace/project",
            "presence": {"state": status, "label": status.title(), "detail": "Reported by the bridge.",
                         "last_seen": now, "bridge_last_seen": now, "bridge_online": True, "can_message": True}}


ANA, BO, CY = "laptop/claude:ana0000001", "laptop/codex:bo00000001", "laptop/claude:cy00000001"


@pytest.fixture
def workspace_ui(paired):
    """#solo has one busy agent, #pair two, #quiet only an idle one; reports are fresh, as the app expires old ones."""
    page, _state = paired
    sessions = [agent(ANA, "ana", "busy"), agent(BO, "bo", "busy"), agent(CY, "cy", "idle")]
    state = {"at": time.time(), "sessions": sessions, "names": {}, "topics": [
        {"topic": "solo", "members": ["user", ANA, CY]},
        {"topic": "pair", "members": [ANA, BO, CY]},
        {"topic": "quiet", "members": [CY]}]}
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": {"pairing": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": [
        {"pair": ANA + " user", "messages": []}]}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": []}))
    return page


def open_view(page, fragment, title):
    page.goto(page.url.split("#")[0] + "#" + fragment, wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text=title).wait_for()


def test_a_busy_agent_shows_as_working_in_its_channel(workspace_ui):
    open_view(workspace_ui, "topic=solo", "solo")
    assert workspace_ui.locator("#working").inner_text() == "@ana is working…"


def test_two_busy_agents_show_together(workspace_ui):
    open_view(workspace_ui, "topic=pair", "pair")
    assert workspace_ui.locator("#working").inner_text() == "@ana and @bo are working…"


def test_a_channel_with_only_idle_agents_shows_nothing(workspace_ui):
    open_view(workspace_ui, "topic=quiet", "quiet")
    assert workspace_ui.locator("#working").is_hidden() is True


def test_a_busy_agent_shows_as_working_in_its_direct_messages(workspace_ui):
    open_view(workspace_ui, "direct=" + ANA + "%20user", "ana")
    assert workspace_ui.locator("#working").inner_text() == "@ana is working…"


def test_the_working_line_does_not_overflow_the_phone(workspace_ui):
    open_view(workspace_ui, "topic=pair", "pair")
    assert workspace_ui.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth") is True
