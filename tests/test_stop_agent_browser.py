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
"""The members panel offers to stop a running agent, behind a second confirming tap."""

import json
import time

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired

AGENT = "laptop/codex:1234567890"


def team(paired, capabilities, status="stopped"):
    """#zulu with one running agent; stop requests are recorded and answered with `status`."""
    page, _state = paired
    now = time.time()
    session = {"address": AGENT, "name": "jack", "status": "idle", "online": True, "cwd": "/workspace/project",
               "presence": {"state": "idle", "label": "Idle", "detail": "", "last_seen": now, "bridge_last_seen": now,
                            "bridge_online": True, "can_message": True}}
    state = {"at": now, "sessions": [session], "topics": [{"topic": "zulu", "members": [AGENT]}], "names": {}}
    stops = []

    def stop(route):
        stops.append(json.loads(route.request.post_data))
        route.fulfill(json={"address": AGENT, "status": status, "id": "a" * 32})
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": capabilities}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": []}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": []}))
    page.route("**/api/sessions/stop", stop)
    page.goto(page.url.split("#")[0] + "#topic=zulu", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text="zulu").wait_for()
    page.locator("#members-toggle").click()
    page.locator("#details .member-row").first.wait_for()
    return page, stops


@pytest.fixture
def panel(paired):
    return team(paired, {"pairing": True, "stop": True})


def test_a_running_agent_has_a_stop_button(panel):
    assert panel[0].locator("#details .stop-agent").inner_text() == "Stop agent"


def test_the_first_tap_asks_for_confirmation(panel):
    page, _ = panel
    page.locator("#details .stop-agent").click()
    assert page.locator("#details .stop-agent").inner_text() == "Tap again to stop @jack"


def test_the_first_tap_stops_nothing(panel):
    page, stops = panel
    page.locator("#details .stop-agent").click()
    page.wait_for_timeout(300)
    assert stops == []


def test_the_second_tap_asks_the_workspace_to_stop_that_agent(panel):
    page, stops = panel
    page.locator("#details .stop-agent").click()
    page.locator("#details .stop-agent").click()
    page.wait_for_function("() => !document.getElementById('toast').hidden")
    assert stops == [{"address": AGENT}]


def test_a_stopped_agent_is_confirmed(panel):
    page, _ = panel
    page.locator("#details .stop-agent").click()
    page.locator("#details .stop-agent").click()
    page.wait_for_function("() => !document.getElementById('toast').hidden")
    assert page.locator("#toast").inner_text() == "@jack stopped."


def test_a_refused_stop_says_why(paired):
    page, _ = team(paired, {"pairing": True, "stop": True}, status="refused")
    page.locator("#details .stop-agent").click()
    page.locator("#details .stop-agent").click()
    page.wait_for_function("() => !document.getElementById('toast').hidden")
    assert "could not end it" in page.locator("#toast").inner_text()


def test_a_workspace_that_cannot_stop_shows_no_button(paired):
    page, _ = team(paired, {"pairing": True})
    assert page.locator("#details .stop-agent").count() == 0
