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
"""On the phone, a routine is added from the sidebar, listed with its schedule, and run on demand."""

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser

OVERFLOW = "() => document.documentElement.scrollWidth <= window.innerWidth"


@pytest.fixture
def phone(browser, real_http_workspace):
    context = browser.new_context(viewport={"width": 390, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block", timezone_id="Europe/Paris")
    page = context.new_page()
    token = AuthStore(real_http_workspace["directory"]).issue_pairing_token()
    page.goto(real_http_workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    yield page, real_http_workspace
    context.close()


@pytest.fixture
def added(phone):
    """A weekday 09:30 routine for #team, added through the form."""
    page, workspace = phone
    page.get_by_role("button", name="Open workspace sidebar").click()
    page.get_by_role("button", name="Add a routine").click()
    page.locator("#routine-name").fill("Standup")
    page.locator("#routine-topic").fill("team")
    page.locator("#routine-when").select_option("weekdays")
    page.locator("#routine-time").fill("09:30")
    page.locator("#routine-prompt").fill("Post the standup summary.")
    page.get_by_role("button", name="Save routine").click()
    page.locator("#routines button").filter(has_text="Standup").wait_for(timeout=15000)
    return page, workspace


def test_an_added_routine_is_stored_with_its_cron_line(added):
    _, workspace = added
    assert workspace["browser"].get("/api/state").json()["routines"][0]["cron"] == "30 9 * * 1-5"


def test_an_added_routine_uses_the_phone_s_time_zone(added):
    _, workspace = added
    assert workspace["browser"].get("/api/state").json()["routines"][0]["tz"] == "Europe/Paris"


def test_an_added_routine_lists_its_schedule_and_channel(added):
    page, _ = added
    assert page.locator("#routines .agent-context").first.inner_text().startswith("Weekdays at 09:30 · #team · next in")


def test_the_routines_list_does_not_overflow(added):
    page, _ = added
    assert page.evaluate(OVERFLOW) is True


@pytest.fixture
def run(added):
    page, workspace = added
    page.locator("#routines button").filter(has_text="Standup").click()
    page.get_by_role("button", name="Run now").click()
    page.locator(".message-body").filter(has_text="Post the standup summary.").wait_for(timeout=15000)
    return page, workspace


def test_run_now_opens_the_channel_with_the_routine_post(run):
    page, _ = run
    assert page.locator("#title").inner_text() == "team"


def test_a_reopened_routine_shows_its_schedule_in_the_form(added):
    page, _ = added
    page.locator("#routines button").filter(has_text="Standup").click()
    assert page.locator("#routine-when").input_value() == "weekdays"


def test_a_paused_routine_says_so_in_the_list(added):
    page, _ = added
    page.locator("#routines button").filter(has_text="Standup").click()
    page.get_by_role("button", name="Pause").click()
    page.locator("#routines .agent-context").filter(has_text="Paused").wait_for(timeout=15000)
    assert page.locator("#routines .agent-context").first.inner_text() == "Paused · #team"
