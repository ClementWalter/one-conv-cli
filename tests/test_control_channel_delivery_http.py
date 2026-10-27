#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
"""A real node's hub acknowledgment reaches the channel view over HTTPS and in the phone browser."""

import pytest

from tests import test_control_browser as phone_tests
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser
phone = phone_tests.phone


def _post_and_accept(workspace):
    worker = workspace["worker"]
    worker.put("/v1/topics/release/members/omp:agent").raise_for_status()
    workspace["browser"].post("/api/messages", json={"id": "a" * 32, "target": "#release", "text": "Ship it."},
                              headers=workspace["headers"]).raise_for_status()
    incoming = next(row for row in worker.get("/v1/inbox").json()["messages"] if row["topic"] == "release")
    worker.post("/v1/acks", json={"delivery": incoming["delivery"], "outcome": "accepted"}).raise_for_status()


@pytest.fixture
def accepted(real_http_workspace):
    _post_and_accept(real_http_workspace)
    response = real_http_workspace["browser"].get("/api/topics/release")
    response.raise_for_status()
    return response.json()["messages"][-1]["delivery"]


def test_api_reports_the_acknowledged_channel_post_as_accepted(accepted):
    assert accepted["state"] == "accepted"


def test_api_never_turns_channel_acceptance_into_a_read(accepted):
    assert accepted["read_at"] is None


@pytest.fixture
def phone_view(phone):
    page, workspace = phone
    _post_and_accept(workspace)
    page.goto(workspace["origin"] + "/#topic=release")
    status = page.locator(".message-delivery").filter(has_text="Accepted by 1 of 1")
    status.wait_for()
    return status


def test_phone_shows_the_channel_acceptance(phone_view):
    assert phone_view.count() == 1


def test_phone_explains_acceptance_is_not_a_read(phone_view):
    assert "reading and a reply are not confirmed" in phone_view.get_attribute("title")
