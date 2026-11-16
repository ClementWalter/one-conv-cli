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
"""The phone asks the hub for a teammate over HTTPS; and a connected spawner starts it."""

import socket
import threading
import time

import pytest
import uvicorn

from one_conv.control.runtime import application
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as phone_tests

tls_certificate = https_tests.tls_certificate
browser = phone_tests.browser


@pytest.fixture
def phone(tmp_path, tls_certificate, browser):
    """A paired phone on a workspace whose Mac offers to start claude sessions."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"https://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, origin)
    app.state.hub.publish("mac", [], spawn="claude", priority=10)
    certificate, key = tls_certificate
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1,
                                          ssl_certfile=str(certificate), ssl_keyfile=str(key)))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Phone staffing fixture did not start")
        time.sleep(0.01)
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True)
    page = context.new_page()
    try:
        page.goto(origin + "/#pair=" + app.state.auth.issue_pairing_token(), wait_until="networkidle")
        page.get_by_role("button", name="Connect this device", exact=True).click()
        page.locator("#pairing").wait_for(state="hidden")
        yield page, app
    finally:
        context.close()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.fixture
def staffed_phone(phone):
    page, app = phone
    page.locator("#staff-team").click()
    page.locator("#staff-name").fill("jack")
    page.locator("#staff-topic").fill("release")
    page.locator("#staff-prompt").fill("Review the current change.")
    with page.expect_response(lambda response: response.url.endswith("/api/providers/workers")) as response:
        page.locator("#staff-submit").click()
    response.value.finished()
    return page, app


def test_the_phone_files_the_teammate_request_in_its_channel(staffed_phone):
    assert [row["text"] for row in staffed_phone[1].state.hub.history("release")] == [
        "New teammate @jack: Review the current change."]


def test_the_phone_says_which_machine_starts_the_teammate(staffed_phone):
    page, _app = staffed_phone
    page.locator("#staff-feedback").filter(has_text="Starting on mac").wait_for()
    assert "#release" in page.locator("#staff-feedback").inner_text()


def test_the_teammate_dialog_asks_only_for_a_name_a_channel_and_a_task(phone):
    page, _app = phone
    page.locator("#staff-team").click()
    assert page.locator("#staff-form input, #staff-form select, #staff-form textarea").count() == 3
