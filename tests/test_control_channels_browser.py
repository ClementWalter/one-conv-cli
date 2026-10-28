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
"""Create a channel from the phone over the real HTTPS workspace in a real browser."""

import pytest

from tests import test_control_browser as phone_tests
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser
phone = phone_tests.phone


def _create(page, purpose, name=""):
    page.get_by_role("button", name="Open workspace sidebar").click()
    page.get_by_role("button", name="Create a channel").click()
    page.locator("#channel-purpose").fill(purpose)
    page.locator("#channel-name").fill(name)
    with page.expect_response("**/api/channels") as response:
        page.get_by_role("button", name="Create channel").click()
    response.value.finished()
    page.locator(".message-body").filter(has_text=purpose).wait_for()


@pytest.fixture
def created(phone, tmp_path):
    page, workspace = phone
    _create(page, "Ship 1.2 this week.", "Release Notes")
    page.screenshot(path=str(tmp_path / "phone-new-channel.png"), full_page=True)
    return page, workspace


def test_typed_name_opens_with_its_case_fixed(created):
    assert created[0].url.endswith("#topic=release-notes")


def test_new_channel_is_announced_in_general(created):
    messages = created[1]["browser"].get("/api/topics/general").json()["messages"]
    assert messages[-1]["text"].startswith("You created #release-notes")


def test_prompt_alone_opens_a_channel_named_from_it(phone):
    _create(phone[0], "Please fix the iPhone keyboard autocorrect.")
    assert phone[0].url.endswith("#topic=fix-iphone-keyboard-autocorrect")


def test_new_channel_dialog_asks_for_no_members(phone):
    phone[0].get_by_role("button", name="Open workspace sidebar").click()
    phone[0].get_by_role("button", name="Create a channel").click()
    assert phone[0].locator("#channel-dialog input[type=checkbox]").count() == 0


def test_new_channel_dialog_fits_a_narrow_phone(phone):
    phone[0].get_by_role("button", name="Open workspace sidebar").click()
    phone[0].get_by_role("button", name="Create a channel").click()
    assert phone[0].locator("#channel-form .field-grid").evaluate("node => node.scrollWidth <= node.clientWidth")
