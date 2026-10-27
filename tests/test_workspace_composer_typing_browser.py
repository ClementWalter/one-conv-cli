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
"""On the phone, typing leaves the message box alone, so iOS keeps its suggestions and autocorrect.

iOS drops the QuickType bar and autocorrect for a field the page rewrites while it is being typed in.
"""

import time

import pytest

from tests import test_pwa_browser as phone_tests

browser = phone_tests.browser
workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired

WATCH = """() => {
  window.composerChanges = [];
  new MutationObserver((records) => window.composerChanges.push(...records.map((r) => r.attributeName)))
    .observe(document.getElementById("message-input"), { attributes: true });
}"""


@pytest.fixture
def typed(paired):
    """Three words typed into #general's composer, one key at a time, with refreshes arriving meanwhile."""
    page, _state = paired
    state = {"at": time.time(), "names": {}, "sessions": [], "topics": [{"topic": "general", "members": ["user"]}]}
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": {"pairing": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json={**state, "at": time.time()}))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": []}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": []}))
    page.goto(page.url.split("#")[0] + "#topic=general", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text="general").wait_for()
    page.locator("#message-input").click()
    page.evaluate(WATCH)
    page.keyboard.type("hello there friend", delay=20)
    page.evaluate("refreshState()")
    page.wait_for_timeout(300)
    return page


def test_typing_changes_no_attribute_of_the_message_box(typed):
    assert typed.evaluate("window.composerChanges") == []


def test_the_typed_text_is_kept(typed):
    assert typed.locator("#message-input").input_value() == "hello there friend"


@pytest.fixture
def grown(typed):
    """Five more lines typed after the words, then the box's height read."""
    start = typed.evaluate("document.getElementById('message-input').offsetHeight")
    for _ in range(5):
        typed.keyboard.press("Shift+Enter")
        typed.keyboard.type("more", delay=10)
    return typed, start, typed.evaluate("document.getElementById('message-input').offsetHeight")


def test_long_text_grows_the_box(grown):
    _, start, height = grown
    assert height > start


def test_deleting_the_text_shrinks_the_box_back(grown):
    page, start, _ = grown
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.press("Backspace")
    assert page.evaluate("document.getElementById('message-input').offsetHeight") <= start
