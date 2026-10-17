"""Create a channel from the phone over the real HTTPS workspace in a real browser."""

import pytest

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
    inbox = created[1]["worker"].get("/v1/inbox").json()["messages"]
    assert any(row["topic"] == "general" and row["text"].startswith("You created #release") for row in inbox)


def test_new_channel_opens_on_the_phone(created):
    assert created[0].url.endswith("#topic=release")
