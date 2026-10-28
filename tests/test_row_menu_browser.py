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
"""Left-panel rows open their actions on a right click: Kill for an agent, Archive for a channel.

Also: a reader at the end of a conversation stays there while the composer grows as they type.
"""

import json
import time

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired

AGENT = "laptop/claude:1234567890"


def messages(count):
    now = time.time()
    return [{"id": f"{i:032x}", "cursor": str(i), "pos": i, "at": now - count + i, "from": AGENT,
             "text": f"Line {i} of a long conversation", "mode": "push"} for i in range(count)]


def workspace_with(paired, capabilities=None, rows=()):
    """#zulu with one running agent `jack`; stop and archive requests are recorded."""
    page, _state = paired
    now = time.time()
    session = {"address": AGENT, "name": "jack", "status": "idle", "online": True, "cwd": "/workspace/project",
               "presence": {"state": "idle", "label": "Idle", "detail": "", "last_seen": now, "bridge_last_seen": now,
                            "bridge_online": True, "can_message": True}}
    state = {"at": now, "sessions": [session], "topics": [{"topic": "zulu", "members": [AGENT]}], "names": {}}
    calls = {"stop": [], "archive": []}

    def record(kind, answer):
        def handle(route):
            calls[kind].append(json.loads(route.request.post_data))
            route.fulfill(json=answer)
        return handle
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True, "csrf_token": "synthetic-csrf",
               "capabilities": capabilities or {"pairing": True, "stop": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": []}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": list(rows)}))
    page.route("**/api/sessions/stop", record("stop", {"address": AGENT, "status": "stopped", "id": "a" * 32}))
    page.route("**/api/channels/archive", record("archive", {"topic": "zulu", "archived": True}))
    page.goto(page.url.split("#")[0] + "#topic=zulu", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text="zulu").wait_for()
    return page, calls


@pytest.fixture
def sidebar(paired):
    page, calls = workspace_with(paired)
    page.locator("#sidebar-toggle").click()
    page.locator("#people .conversation-row").first.wait_for()
    return page, calls


def agent_menu(page):
    page.locator("#people .conversation-row").first.click(button="right")
    return page.locator("#row-menu .row-menu-item")


def test_right_clicking_an_agent_offers_to_kill_it(sidebar):
    assert agent_menu(sidebar[0]).all_inner_texts() == ["Kill @jack"]


def test_the_first_kill_tap_asks_for_confirmation(sidebar):
    page, _ = sidebar
    agent_menu(page).click()
    assert page.locator("#row-menu .row-menu-item").inner_text() == "Tap again to kill @jack"


def test_the_first_kill_tap_kills_nothing(sidebar):
    page, calls = sidebar
    agent_menu(page).click()
    page.wait_for_timeout(300)
    assert calls["stop"] == []


def test_the_second_kill_tap_stops_that_agent(sidebar):
    page, calls = sidebar
    agent_menu(page).click()
    page.locator("#row-menu .row-menu-item").click()
    page.wait_for_function("() => !document.getElementById('toast').hidden")
    assert calls["stop"] == [{"address": AGENT}]


def test_the_menu_closes_on_escape(sidebar):
    page, _ = sidebar
    agent_menu(page).first.wait_for()
    page.keyboard.press("Escape")
    assert page.locator("#row-menu").count() == 0


def test_a_long_press_opens_the_agent_menu(sidebar):
    page, _ = sidebar
    page.locator("#people .conversation-row").first.dispatch_event("touchstart", {"touches": [{"clientX": 40, "clientY": 300, "identifier": 0}]})
    page.locator("#row-menu").wait_for(timeout=2000)
    assert page.locator("#row-menu .row-menu-item").all_inner_texts() == ["Kill @jack"]


def test_right_clicking_a_channel_offers_to_archive_it(sidebar):
    page, _ = sidebar
    page.locator("#topics .conversation-row").first.click(button="right")
    assert page.locator("#row-menu .row-menu-item").all_inner_texts() == ["Archive channel"]


def test_archive_from_the_menu_archives_that_channel(sidebar):
    page, calls = sidebar
    page.locator("#topics .conversation-row").first.click(button="right")
    page.locator("#row-menu .row-menu-item").click()
    page.wait_for_function("() => !document.getElementById('toast').hidden")
    assert calls["archive"] == [{"topic": "zulu", "archived": True}]


def test_a_workspace_that_cannot_stop_offers_no_kill(paired):
    page, _ = workspace_with(paired, {"pairing": True})
    page.locator("#sidebar-toggle").click()
    page.locator("#people .conversation-row").first.click(button="right")
    page.wait_for_timeout(200)
    assert page.locator("#row-menu").count() == 0


# --- staying at the end while typing -------------------------------------------------------------------


def test_typing_lines_keeps_the_conversation_at_its_end(paired):
    page, _ = workspace_with(paired, rows=messages(80))
    page.locator("#messages").filter(has_text="Line 79").wait_for()
    page.locator("#message-input").click()
    page.keyboard.type("first line")
    for _ in range(5):
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("another line")
    page.wait_for_timeout(200)
    assert page.evaluate("() => { const box = document.getElementById('message-scroll');"
                         " return box.scrollHeight - box.scrollTop - box.clientHeight; }") < 2
