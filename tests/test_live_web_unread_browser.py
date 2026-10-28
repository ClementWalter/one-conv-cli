#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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
"""Unread channels and direct messages turn bold in a real browser and clear when the human opens them."""

import os
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from one_conv.live import router, team
from one_conv.live.model import LiveSession
from tests import test_live_web_browser as browser_tests

workspace = browser_tests.workspace
PHONE = 600


@pytest.fixture(scope="module", params=["webkit", "chromium"])
def browser(request):
    """Both engines render the same production assets in disposable contexts."""
    with playwright.sync_playwright() as runtime:
        options = {"headless": True}
        if request.param == "chromium" and os.getenv("ONE_CONV_TEST_BROWSER"):
            options["executable_path"] = os.environ["ONE_CONV_TEST_BROWSER"]
        try:
            instance = getattr(runtime, request.param).launch(**options)
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip(f"Install the Playwright {request.param} browser")
            raise
        yield instance
        instance.close()


@pytest.fixture(params=[390, 1280])
def width(request):
    return request.param


def new_page(browser, workspace, width):
    phone = width < PHONE
    context = browser.new_context(viewport={"width": width, "height": 740 if phone else 900},
                                  is_mobile=phone, has_touch=phone)
    active = context.new_page()
    active.goto(workspace["url"])
    active.locator(".message").first.wait_for()
    return context, active


def open_sidebar(page):
    """A phone keeps the sidebar behind its menu button; a desktop always shows it."""
    if page.locator("#sidebar-toggle").is_visible():
        page.locator("#sidebar-toggle").click()
        page.locator(".sidebar").evaluate("node => Promise.all(node.getAnimations().map(a => a.finished))")


@pytest.fixture
def page(browser, workspace, width):
    context, active = new_page(browser, workspace, width)
    yield active
    context.close()


@pytest.fixture
def channel_news(page):
    """An agent posts in a channel the human does not have open."""
    team.join("release-notes", "omp:browser-builder")
    router.send("#release-notes", "Version 2 is out.", LiveSession("omp", "browser-builder"))
    row = page.locator(".conversation-row.unread").filter(has_text="release-notes")
    row.wait_for(state="attached", timeout=15000)
    open_sidebar(page)
    return row


@pytest.fixture
def direct_news(page):
    router.send("user", "A question for you.", LiveSession("omp", "browser-builder"))
    row = page.locator("#people .conversation-row.unread").filter(has_text="builder")
    row.wait_for(state="attached", timeout=15000)
    open_sidebar(page)
    return row


def test_unread_channel_is_bold(channel_news):
    assert int(channel_news.evaluate("node => getComputedStyle(node).fontWeight")) >= 700


def test_unread_channel_shows_its_count(channel_news):
    assert channel_news.locator(".row-unread").inner_text() == "1"


def test_unread_count_is_in_the_tab_title(page, channel_news):
    assert page.title().startswith("(1) ")


def test_menu_button_carries_the_unread_mark(page, channel_news):
    assert "has-unread" in page.locator("#sidebar-toggle").get_attribute("class")


def test_opening_a_channel_clears_it(page, channel_news):
    channel_news.click()
    page.locator(".message-body").filter(has_text="Version 2 is out.").wait_for()
    page.locator(".conversation-row.unread").filter(has_text="release-notes").wait_for(state="detached")
    assert page.locator(".conversation-row.unread").count() == 0


def test_a_cleared_channel_stays_read_on_another_device(browser, workspace, width, page, channel_news):
    with page.expect_response("**/api/read"):
        channel_news.click()
    context, other = new_page(browser, workspace, width)
    other.locator(".conversation-row").filter(has_text="release-notes").wait_for(state="attached")
    unread = other.locator(".conversation-row.unread").count()
    context.close()
    assert unread == 0


def test_unread_direct_message_shows_its_count(direct_news):
    assert direct_news.locator(".row-unread").inner_text() == "1"


def test_opening_a_direct_message_clears_it(page, direct_news):
    direct_news.click()
    page.locator(".message-body").filter(has_text="A question for you.").wait_for()
    page.locator("#people .conversation-row.unread").wait_for(state="detached")
    assert page.locator(".conversation-row.unread").count() == 0


def test_the_open_channel_never_turns_bold(page):
    team.record("design-review", "c" * 32, "omp:browser-builder", "Another update.")
    page.locator(".message-body").filter(has_text="Another update.").wait_for(timeout=15000)
    time.sleep(0.5)
    assert page.locator(".conversation-row.unread").count() == 0
