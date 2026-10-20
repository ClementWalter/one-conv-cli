"""Reported agent DMs on the phone: the thread, the channel, and the recipient marker on grouped bubbles."""

import time

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_browser as phone_tests
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser
phone = phone_tests.phone


@pytest.fixture
def reported(phone):
    """The same agent sends two #release-tagged DMs to different peers a minute apart."""
    page, workspace = phone
    at = time.time() - 600
    workspace["worker"].post("/v1/history", json={"messages": [
        {"id": "1" * 32, "from": "omp:agent", "to": "codex:alpha", "topic": "release", "text": "Alpha, rerun the suite.",
         "at": at, "outcome": "accepted"},
        {"id": "2" * 32, "from": "omp:agent", "to": "codex:beta", "topic": "release", "text": "Beta, update the docs.",
         "at": at + 60, "outcome": "accepted"},
    ]}).raise_for_status()
    # A hash change alone would not reload the direct threads; the change feed rules out network idle.
    page.goto(workspace["origin"] + "/#topic=release")
    page.reload()
    page.locator(".message-body").filter(has_text="Beta, update the docs.").wait_for()
    return page


def test_each_tagged_dm_names_its_recipient(reported):
    assert reported.locator(".message-directed").count() == 2


def test_the_grouped_second_dm_still_shows_its_recipient(reported):
    assert reported.locator(".message-directed").nth(1).is_visible()


def test_the_marker_says_only_the_recipient_was_notified(reported):
    assert reported.locator(".message-directed").nth(1).inner_text().endswith("only")


def test_the_workspace_has_no_direct_messages_list(reported):
    assert reported.locator("#threads").count() == 0


def test_the_grouped_second_dm_says_it_is_synced_history(reported):
    assert reported.locator(".message-synced").nth(1).is_visible()


@pytest.fixture(params=[390])
def long_recipient(browser, real_http_workspace, request):
    """A tagged DM to an agent with the longest allowed name, on a narrow phone."""
    workspace = real_http_workspace
    workspace["worker"].put("/v1/sessions", json={"sessions": [
        {"address": "omp:agent", "name": "builder", "status": "idle"},
        {"address": "codex:alpha", "name": "m" * 32, "status": "idle"}]}).raise_for_status()
    workspace["worker"].post("/v1/history", json={"messages": [
        {"id": "3" * 32, "from": "omp:agent", "to": "codex:alpha", "topic": "release", "text": "Rerun the suite.",
         "at": time.time() - 60, "outcome": "accepted"}]}).raise_for_status()
    context = browser.new_context(viewport={"width": request.param, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True)
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    page.goto(workspace["origin"] + "/#topic=release")
    page.reload()
    page.locator(".message-directed").wait_for()
    yield page
    context.close()


def test_a_long_recipient_does_not_widen_the_message_list(long_recipient):
    assert long_recipient.locator("#message-scroll").evaluate("node => node.scrollWidth <= node.clientWidth")


def test_a_long_recipient_marker_stays_inside_the_list(long_recipient):
    assert long_recipient.locator(".message-directed").evaluate(
        "node => node.getBoundingClientRect().right <= "
        "document.getElementById('message-scroll').getBoundingClientRect().right")


def test_a_long_recipient_marker_shows_the_full_name(long_recipient):
    assert long_recipient.locator(".message-directed").inner_text() == "To @" + "m" * 32 + " only"
