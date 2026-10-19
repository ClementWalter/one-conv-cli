"""Create a channel from the phone over the real HTTPS workspace in a real browser."""

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
def created(phone, tmp_path):
    page, workspace = phone
    page.get_by_role("button", name="Open workspace sidebar").click()
    page.get_by_role("button", name="Create a channel").click()
    page.locator("#channel-name").fill("release")
    page.locator("#channel-purpose").fill("Ship 1.2 this week.")
    page.locator("#channel-members label").filter(has_text="@builder").locator("input").check()
    with page.expect_response("**/api/channels") as response:
        page.get_by_role("button", name="Create channel").click()
    response.value.finished()
    page.locator(".message-body").filter(has_text="Ship 1.2 this week.").wait_for()
    page.screenshot(path=str(tmp_path / "phone-new-channel.png"), full_page=True)
    return page, workspace


def test_new_channel_kickoff_reaches_the_chosen_agent(created):
    inbox = created[1]["worker"].get("/v1/inbox").json()["messages"]
    assert {(row["topic"], row["text"]) for row in inbox} >= {("release", "Ship 1.2 this week.")}


def test_new_channel_is_announced_in_general(created):
    messages = created[1]["browser"].get("/api/topics/general").json()["messages"]
    assert messages[-1]["text"].startswith("You created #release")


def test_new_channel_opens_on_the_phone(created):
    assert created[0].url.endswith("#topic=release")


@pytest.fixture(params=[320, 390])
def long_member_dialog(browser, real_http_workspace, request):
    """A member with a long native title and project path, in the dialog on a narrow phone."""
    workspace = real_http_workspace
    workspace["worker"].put("/v1/sessions", json={"sessions": [
        {"address": "omp:agent", "name": "builder", "status": "idle",
         "title": "ReviewTheEntireCloudWorkspaceMembershipFlow" * 6,
         "cwd": "/" + "verylongdirectoryname" * 12}]}).raise_for_status()
    context = browser.new_context(viewport={"width": request.param, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True)
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    page.get_by_role("button", name="Open workspace sidebar").click()
    page.get_by_role("button", name="Create a channel").click()
    page.locator("#channel-members label").first.wait_for()
    yield page
    context.close()


def test_long_member_labels_fit_the_dialog(long_member_dialog):
    assert long_member_dialog.locator("#channel-dialog").evaluate("node => node.scrollWidth <= node.clientWidth")


def test_long_member_labels_fit_the_form(long_member_dialog):
    assert long_member_dialog.locator("#channel-form .field-grid").evaluate(
        "node => node.scrollWidth <= node.clientWidth")


def test_member_checkbox_keeps_a_tappable_size(long_member_dialog):
    box = long_member_dialog.locator("#channel-members input").first.bounding_box()
    assert box["width"] >= 16


def test_member_checkbox_can_be_checked(long_member_dialog):
    long_member_dialog.locator("#channel-members input").first.check()
    assert long_member_dialog.locator("#channel-members input").first.is_checked()
