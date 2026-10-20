"""On the phone, at 320 and 390 px: archiving a channel folds it away, and signing out other devices keeps this one."""

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser

OVERFLOW = "() => document.documentElement.scrollWidth <= window.innerWidth"


@pytest.fixture
def workspace(real_http_workspace):
    """#release exists with one post from the human."""
    real_http_workspace["browser"].post(
        "/api/messages", json={"id": "a" * 32, "target": "#release", "text": "Ship the release notes today."},
        headers=real_http_workspace["headers"]).raise_for_status()
    return real_http_workspace


@pytest.fixture(params=[320, 390])
def phone(browser, workspace, request):
    context = browser.new_context(viewport={"width": request.param, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block")
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    page.goto("about:blank")
    page.goto(workspace["origin"] + "/#topic=release")
    page.locator(".message-body").filter(has_text="Ship the release notes today.").wait_for()
    yield page, workspace
    context.close()


@pytest.fixture
def archived(phone):
    page, workspace = phone
    page.locator(".archive-channel").click()
    page.locator("#archived-section").wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="Open workspace sidebar").click()
    return page, workspace


def test_an_archived_channel_leaves_the_channel_list(archived):
    page, _ = archived
    assert page.locator("#topics button").filter(has_text="release").count() == 0


def test_an_archived_channel_is_listed_under_archived(archived):
    page, _ = archived
    page.locator('[data-section="archived-topics"]').click()
    assert page.locator("#archived-topics button").filter(has_text="release").count() == 1


def test_an_archived_channel_is_stored_as_archived(archived):
    _, workspace = archived
    topics = workspace["browser"].get("/api/state").json()["topics"]
    assert next(topic for topic in topics if topic["topic"] == "release")["archived"] is True


def test_the_archived_list_does_not_overflow(archived):
    page, _ = archived
    assert page.evaluate(OVERFLOW) is True


@pytest.fixture
def signed_out(phone):
    """Another browser is paired too; this phone signs it out."""
    page, workspace = phone
    page.locator("#device-menu").click()
    page.locator("#sign-out-others").click()
    page.locator("#sign-out-feedback").filter(has_text="signed out").wait_for(timeout=15000)
    return page, workspace


def test_signing_out_others_disconnects_the_other_browser(signed_out):
    _, workspace = signed_out
    assert workspace["browser"].get("/api/state").status_code == 401


def test_signing_out_others_keeps_this_phone(signed_out):
    page, _ = signed_out
    assert page.evaluate("async () => (await fetch('/api/state')).status") == 200
