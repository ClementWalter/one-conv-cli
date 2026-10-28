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
"""Pair and exchange human messages with the actual cloud API over HTTPS in real browsers."""

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as phone_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = phone_tests.browser


@pytest.fixture
def phone(browser, real_http_workspace):
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True)
    page = context.new_page()
    token = AuthStore(real_http_workspace["directory"]).issue_pairing_token()
    page.goto(real_http_workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    yield page, real_http_workspace
    context.close()


def test_real_cloud_pairing_creates_a_secure_phone_session(phone):
    assert phone[0].context.cookies()[0]["secure"] is True


def test_real_cloud_pairing_does_not_leave_a_token_in_the_url(phone):
    assert "pair=" not in phone[0].url


@pytest.fixture
def sent(phone):
    page, workspace = phone
    page.get_by_role("button", name="Open workspace sidebar").click()
    page.get_by_role("button", name="New message", exact=True).click()
    page.locator(".recipient-option").filter(has_text="builder").click()
    page.locator("#message-input").fill("A task from the phone.")
    with page.expect_response("**/api/messages") as response:
        page.get_by_role("button", name="Send message as a human").click()
    response.value.finished()
    incoming = workspace["worker"].get("/v1/inbox").json()["messages"][0]
    return page, workspace, incoming


def test_real_phone_send_is_human_at_the_remote_boundary(sent):
    assert sent[2]["from"] == "workspace/user"


def test_real_phone_send_preserves_the_task(sent):
    assert sent[2]["text"] == "A task from the phone."


def test_real_remote_reply_appears_on_the_phone(sent, tmp_path):
    page, workspace, _incoming = sent
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "workspace/user",
                                                  "text": "Reply from a different machine."}).raise_for_status()
    answer = page.locator(".message-body").filter(has_text="Reply from a different machine.")
    answer.wait_for()
    page.screenshot(path=str(tmp_path / "phone-cloud-chat.png"), full_page=True)
    assert answer.count() == 1


def test_real_phone_logout_revokes_its_session(phone):
    page, workspace = phone
    page.get_by_role("button", name="Device settings").click()
    page.get_by_role("button", name="Disconnect this device").click()
    page.locator("#pairing").wait_for()
    assert page.request.get(workspace["origin"] + "/api/session").json()["authenticated"] is False
