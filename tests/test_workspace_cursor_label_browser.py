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


def test_a_cursor_avatar_shows_the_cursor_logo(workspace_ui):
    assert workspace_ui.locator("#people .avatar.cursor svg.harness-logo").count() == 2
