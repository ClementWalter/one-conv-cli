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
"""An open app picks up a deploy by itself: it reloads once the change feed reports a new version, unless busy."""

import pytest

from one_conv.live import web
from tests import test_control_messages as https_tests
from tests import test_control_unread_browser as unread_tests

pytest.importorskip("playwright.sync_api")

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = unread_tests.browser


@pytest.fixture
def page(browser, real_http_workspace):
    real_http_workspace["worker"].put("/v1/topics/general/members/omp:agent").raise_for_status()
    context, active = unread_tests.paired(browser, real_http_workspace, "topic=general", 1280)
    active.locator("#composer:not([hidden]) #message-input").wait_for()
    # Survives only as long as this document does: a reload clears it.
    active.evaluate("() => { window.__loaded = 'before the deploy'; }")
    yield active
    context.close()


def deploy(monkeypatch, workspace, ident="d"):
    """A new version is served, and an agent posts so the change feed answers at once."""
    monkeypatch.setattr(web, "VERSION", "a-new-version")
    workspace["worker"].post("/v1/messages", json={"id": ident * 32, "from": "omp:agent", "to": "#general",
                                                   "text": "Deployed."}).raise_for_status()


def reloaded(page):
    return page.evaluate("() => window.__loaded === undefined")


def test_an_idle_app_reloads_after_a_deploy(page, real_http_workspace, monkeypatch):
    page.locator("#message-input").evaluate("node => node.blur()")
    deploy(monkeypatch, real_http_workspace)
    page.wait_for_function("() => window.__loaded === undefined", timeout=40000)
    assert reloaded(page)


def test_an_app_with_a_draft_waits(page, real_http_workspace, monkeypatch):
    page.locator("#message-input").fill("half written")
    page.locator("#message-input").evaluate("node => node.blur()")
    deploy(monkeypatch, real_http_workspace)
    page.locator(".message-body", has_text="Deployed.").wait_for(timeout=40000)
    page.wait_for_timeout(1000)
    assert not reloaded(page)


def test_the_app_reloads_once_the_draft_is_cleared(page, real_http_workspace, monkeypatch):
    page.locator("#message-input").fill("half written")
    page.locator("#message-input").evaluate("node => node.blur()")
    deploy(monkeypatch, real_http_workspace)
    page.locator(".message-body", has_text="Deployed.").wait_for(timeout=40000)
    page.locator("#message-input").fill("")
    page.locator("#message-input").evaluate("node => node.blur()")
    page.wait_for_function("() => window.__loaded === undefined", timeout=10000)
    assert reloaded(page)


def test_the_change_feed_reports_the_served_version(real_http_workspace):
    response = real_http_workspace["browser"].get("/api/changes?wait=0", headers=real_http_workspace["headers"])
    assert response.json()["version"] == web.VERSION
