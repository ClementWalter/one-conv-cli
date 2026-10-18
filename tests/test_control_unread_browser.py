"""Cloud unread marks over real HTTPS: a channel read on one paired device clears on the other."""

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser


def paired(browser, workspace, hash_route):
    context = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True,
                                  service_workers="block")
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    # The change feed keeps one request open, so wait for elements rather than network idle.
    page.goto(workspace["origin"] + "/#" + hash_route)
    page.reload()
    page.locator(".conversation-row").first.wait_for()
    return context, page


@pytest.fixture
def devices(browser, real_http_workspace):
    workspace = real_http_workspace
    worker = workspace["worker"]
    worker.put("/v1/topics/release/members/omp:agent").raise_for_status()
    first_context, first = paired(browser, workspace, "topic=general")
    second_context, second = paired(browser, workspace, "topic=general")
    worker.post("/v1/messages", json={"id": "e" * 32, "from": "omp:agent", "to": "#release",
                                      "text": "Release notes are ready."}).raise_for_status()
    for page in (first, second):
        page.locator(".conversation-row.unread").filter(has_text="release").wait_for(timeout=15000)
    yield first, second
    first_context.close()
    second_context.close()


def test_an_agent_post_is_unread_on_every_device(devices):
    _, second = devices
    assert second.locator(".conversation-row.unread .row-unread").inner_text() == "1"


def test_reading_on_one_device_clears_the_other(devices):
    first, second = devices
    first.locator(".conversation-row").filter(has_text="release").click()
    first.locator(".message-body").filter(has_text="Release notes are ready.").wait_for()
    second.locator(".conversation-row.unread").wait_for(state="detached", timeout=15000)
    assert second.locator(".conversation-row.unread").count() == 0
