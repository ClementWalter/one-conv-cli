"""Unread channels and direct messages turn bold in a real browser and clear when the human opens them."""

import time

import pytest

pytest.importorskip("playwright.sync_api")

from one_conv.live import router, team
from one_conv.live.model import LiveSession
from tests import test_live_web_browser as browser_tests

browser = browser_tests.browser
workspace = browser_tests.workspace


@pytest.fixture
def page(browser, workspace):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    active = context.new_page()
    active.goto(workspace["url"])
    active.locator(".message").first.wait_for()
    yield active
    context.close()


@pytest.fixture
def channel_news(page):
    """An agent posts in a channel the human does not have open."""
    team.join("release-notes", "omp:browser-builder")
    router.send("#release-notes", "Version 2 is out.", LiveSession("omp", "browser-builder"))
    row = page.locator(".conversation-row.unread").filter(has_text="release-notes")
    row.wait_for(timeout=15000)
    return row


@pytest.fixture
def direct_news(page):
    router.send("user", "A question for you.", LiveSession("omp", "browser-builder"))
    row = page.locator("#threads .conversation-row.unread").filter(has_text="builder")
    row.wait_for(timeout=15000)
    return row


def test_unread_channel_is_bold(channel_news):
    assert int(channel_news.evaluate("node => getComputedStyle(node).fontWeight")) >= 700


def test_unread_channel_shows_its_count(channel_news):
    assert channel_news.locator(".row-unread").inner_text() == "1"


def test_unread_count_is_in_the_tab_title(page, channel_news):
    assert page.title().startswith("(1) ")


def test_opening_a_channel_clears_it(page, channel_news):
    channel_news.click()
    page.locator(".message-body").filter(has_text="Version 2 is out.").wait_for()
    page.locator(".conversation-row.unread").filter(has_text="release-notes").wait_for(state="detached")
    assert page.locator(".conversation-row.unread").count() == 0


def test_a_cleared_channel_stays_read_on_another_device(browser, workspace, page, channel_news):
    with page.expect_response("**/api/read"):
        channel_news.click()
    other = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    other.goto(workspace["url"])
    other.locator(".conversation-row").filter(has_text="release-notes").wait_for()
    assert other.locator(".conversation-row.unread").count() == 0


def test_unread_direct_message_shows_its_count(direct_news):
    assert direct_news.locator(".row-unread").inner_text() == "1"


def test_opening_a_direct_message_clears_it(page, direct_news):
    direct_news.click()
    page.locator(".message-body").filter(has_text="A question for you.").wait_for()
    page.locator("#threads .conversation-row.unread").wait_for(state="detached")
    assert page.locator(".conversation-row.unread").count() == 0


def test_the_open_channel_never_turns_bold(page):
    team.record("design-review", "c" * 32, "omp:browser-builder", "Another update.")
    page.locator(".message-body").filter(has_text="Another update.").wait_for(timeout=15000)
    time.sleep(0.5)
    assert page.locator(".conversation-row.unread").count() == 0
