"""A paired desktop invites a phone through authenticated production HTTPS endpoints."""

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_browser as cloud_tests

browser = cloud_tests.browser
tls_certificate = cloud_tests.tls_certificate
real_http_workspace = cloud_tests.real_http_workspace


@pytest.fixture
def desktop(browser, real_http_workspace):
    """A disposable desktop session is independent of the newly invited phone."""
    context = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True,
                                  service_workers="block")
    page = context.new_page()
    invitation = AuthStore(real_http_workspace["directory"]).issue_pairing_token()
    page.goto(real_http_workspace["origin"] + "/#pair=" + invitation, wait_until="networkidle")
    page.locator("#pair-device").click()
    page.locator("#pairing").wait_for(state="hidden")
    page.locator("#device-menu").click()
    yield page, real_http_workspace
    context.close()


def test_real_settings_open_does_not_replace_a_pending_link(desktop):
    page, workspace = desktop
    page.locator("[data-close=device-dialog]").click()
    pending = AuthStore(workspace["directory"]).issue_pairing_token()
    page.locator("#device-menu").click()
    assert AuthStore(workspace["directory"]).pair(pending) is not None


@pytest.fixture
def desktop_invitation(desktop):
    page, workspace = desktop
    page.locator("#generate-invitation").click()
    page.locator("#device-invitation").wait_for()
    return page, workspace, page.locator("#device-invitation-url").input_value()


@pytest.fixture
def invited_phone(browser, desktop_invitation):
    desktop, workspace, invitation = desktop_invitation
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block")
    phone = context.new_page()
    phone.goto(invitation, wait_until="networkidle")
    phone.locator("#pair-device").click()
    phone.locator("#pairing").wait_for(state="hidden")
    yield desktop, phone, workspace, invitation
    context.close()


def test_desktop_generated_link_connects_the_phone(invited_phone):
    _desktop, phone, workspace, _invitation = invited_phone
    assert phone.request.get(workspace["origin"] + "/api/session").json()["authenticated"] is True


def test_connecting_phone_keeps_the_desktop_session(invited_phone):
    desktop, _phone, workspace, _invitation = invited_phone
    assert desktop.request.get(workspace["origin"] + "/api/session").json()["authenticated"] is True


def test_phone_consumes_the_link_only_once(invited_phone, browser):
    _desktop, _phone, workspace, invitation = invited_phone
    context = browser.new_context(ignore_https_errors=True)
    try:
        result = context.request.post(workspace["origin"] + "/api/login", data={"token": invitation.split("#pair=")[1]},
                                      headers={"Origin": workspace["origin"]})
        assert result.status == 401
    finally:
        context.close()


def test_phone_hides_consumed_invitation_from_its_location(invited_phone):
    assert "pair=" not in invited_phone[1].url


def test_desktop_closing_settings_discards_the_visible_secret(desktop_invitation):
    page = desktop_invitation[0]
    page.locator("[data-close=device-dialog]").click()
    assert page.locator("#device-invitation-url").input_value() == ""


def test_device_settings_fit_an_iphone(invited_phone, tmp_path):
    phone = invited_phone[1]
    phone.locator("#device-menu").click()
    phone.locator("#generate-invitation").click()
    phone.locator("#device-invitation").wait_for()
    phone.screenshot(path=str(tmp_path / "iphone-device-settings.png"), full_page=True)
    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth")
