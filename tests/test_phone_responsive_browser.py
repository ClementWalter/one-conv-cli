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
"""Phone layouts keep long workspace content and controls inside both browser viewports."""

import os
import time

import pytest

from tests import test_live_web_browser as workspace_tests
from tests import test_pwa_browser as phone_tests

workspace = phone_tests.workspace
cloud_page = phone_tests.cloud_page
paired = phone_tests.paired


@pytest.fixture(scope="module", params=["webkit", "chromium"])
def browser(request):
    """Both engines render production assets with disposable mobile contexts."""
    with workspace_tests.playwright.sync_playwright() as runtime:
        options = {"headless": True}
        if request.param == "chromium" and os.getenv("ONE_CONV_TEST_BROWSER"):
            options["executable_path"] = os.environ["ONE_CONV_TEST_BROWSER"]
        instance = getattr(runtime, request.param).launch(**options)
        yield instance
        instance.close()


@pytest.fixture(params=[320, 375, 390, 430])
def phone_width(request):
    return request.param


@pytest.fixture
def long_workspace(paired, phone_width):
    """Bounded literal labels and URLs exercise content-driven minimum sizes."""
    page, cloud = paired
    page.set_viewport_size({"width": phone_width, "height": 844})
    now = time.time()
    topic = "release-" + "planning" * 7
    address = "development-machine/codex:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    agent = {"address": address, "name": "reviewer-" + "x" * 23, "cwd": "/workspace/" + "project" * 14,
             "title": "Internal teammate: /root/" + "reviewer_" * 20, "status": "busy", "online": True,
             "presence": {"state": "busy", "label": "Busy", "detail": "The native agent reports activity. " * 8,
                          "last_seen": now, "bridge_last_seen": now, "bridge_online": True, "can_message": True}}
    text = "https://example.invalid/" + "a" * 350 + "\n\n```\n" + "0123456789" * 60 + "\n```"
    rows = [{"id": "a" * 32, "from": address, "to": "user", "text": text, "at": now}]
    state = {"at": now, "topics": [{"topic": topic, "members": [address]}], "sessions": [agent], "names": {}}
    page.route("**/api/session", lambda route: route.fulfill(json={"authenticated": True,
               "csrf_token": "synthetic-csrf", "capabilities": {"pairing": True, "workers": True, "accounting": True}}))
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    page.route("**/api/direct", lambda route: route.fulfill(json={"threads": [{"pair": address + " user", "messages": rows}]}))
    page.route("**/api/topics/*", lambda route: route.fulfill(json={"messages": rows}))
    page.route("**/api/devices/pair", lambda route: route.fulfill(json={"url": page.url.split("#")[0] + "#pair=" + "p" * 43,
               "expires_at": now + 600, "single_use": True, "access": "owner"}))
    cloud["profiles"] = {"default": {"label": "RepositoryAccess" * 16}}
    cloud["jobs"] = [{"id": "b" * 32, "status": "unconfirmed", "phase": "allocation-blocked",
                       "blocked_by_pool": "unresolved", "spec": {"name": "reviewer-" + "r" * 23, "profile": "default"},
                       "error": "https://example.invalid/" + "error_" * 70}]
    page.route("**/api/providers/accounting", lambda route: route.fulfill(json={
        "cards": [{"cost_center_id": "reviewer", "name": "AccountingOwner" * 12, "hourly_rate": {
            "amount": "123456789.12345678", "currency": "USD", "basis": "allocated",
            "metering_basis": "Occupied work hour including internal subagents"}}],
        "events": [{"cost_center_id": "reviewer", "model": "provider/" + "model" * 30,
                    "tokens": {"total": 1200}, "cash_cost": {"amount": None}}]}))
    page.goto(page.url.split("#")[0] + "#topic=" + topic)
    page.reload(wait_until="networkidle")
    page.locator("#title").filter(has_text=topic).wait_for()
    return page, address, phone_width


@pytest.fixture(params=["topic", "direct", "people", "device", "staffing", "accounting", "recipients"])
def phone_screen(long_workspace, request):
    """Each screen is opened through its actual visible production control."""
    page, _address, _width = long_workspace
    selectors = [".topbar", ".workspace"]
    important = "#send-message"
    if request.param in ("direct", "people"):
        page.locator("#sidebar-toggle").click()
        page.locator("#people .conversation-row").click()
        page.locator(".sidebar").evaluate("node => Promise.all(node.getAnimations().map(animation => animation.finished))")
    if request.param in ("topic", "direct"):
        page.locator(".show-more").click()
        # The resting composer is one input line; its send button is the primary control once in use.
        page.locator("#message-input").focus()
        page.locator("#send-message").wait_for(state="visible")
        selectors += [".conversation", ".conversation-header", ".conversation-intro", ".message-scroll", ".composer-area"]
    elif request.param == "people":
        page.locator("#members-toggle").click()
        selectors += ["#details", "#details-content", ".member-row"]
        important = "#details-close"
    elif request.param == "device":
        page.locator("#device-menu").click()
        page.locator("#generate-invitation").click()
        page.locator("#device-invitation").wait_for()
        selectors += ["#device-dialog", ".device-connect"]
        important = "#copy-invitation"
    elif request.param == "staffing":
        page.locator("#staff-team").click()
        page.locator(".worker-job").wait_for()
        selectors += ["#staff-dialog", ".field-grid", ".worker-job"]
        important = "#staff-submit"
    elif request.param == "accounting":
        page.locator("#show-costs").click()
        page.locator(".rate-card").wait_for()
        page.get_by_role("button", name="Set planning rate", exact=True).click()
        selectors += ["#costs-dialog", ".rate-card", ".quote-form", ".usage-row"]
        important = "#costs-dialog button[type=submit]"
    elif request.param == "recipients":
        page.locator("#sidebar-toggle").click()
        page.locator("#new-message").click()
        selectors += ["#message-dialog", ".recipient-list", ".recipient-option"]
        important = "#dialog-close"
    return page, selectors, important, request.param, long_workspace[2]


def test_phone_document_and_content_do_not_scroll_sideways(phone_screen, tmp_path):
    page, selectors, _important, screen, width = phone_screen
    if width in (320, 390):
        page.screenshot(path=str(tmp_path / (screen + ".png")), full_page=True)
    assert page.evaluate("""({selectors, width}) => {
      const nodes = [document.documentElement, document.body, ...selectors.flatMap(selector => [...document.querySelectorAll(selector)])];
      const failures = nodes.filter(node => node.scrollWidth > node.clientWidth + 1 || node.getBoundingClientRect().right > width + 1)
        .map(node => ({node: node.id || node.className || node.tagName, width: node.clientWidth, scroll: node.scrollWidth, right: node.getBoundingClientRect().right}));
      if (innerWidth > width + 1) failures.push({node:'viewport', actual:innerWidth, configured:width});
      return failures;
    }""", {"selectors": selectors, "width": width}) == []


def test_phone_primary_controls_remain_reachable(phone_screen):
    page, _selectors, important, _screen, width = phone_screen
    button = page.locator(important).last
    button.scroll_into_view_if_needed()
    assert button.evaluate("""(node,width) => {
      const r=node.getBoundingClientRect();
      const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
      return r.width>0 && r.left>=0 && r.right<=width && r.top>=0 && r.bottom<=innerHeight && node.contains(hit);
    }""", width)


def test_phone_editable_controls_use_a_focus_safe_font(phone_screen):
    page = phone_screen[0]
    assert page.locator("input, textarea, select").evaluate_all("nodes => nodes.filter(node => node.getClientRects().length && !node.readOnly && parseFloat(getComputedStyle(node).fontSize)<16).map(node=>node.id || node.name)") == []


def test_phone_code_scrolls_within_the_message(long_workspace):
    page = long_workspace[0]
    page.locator(".show-more").click()
    assert page.locator(".message-body pre").evaluate("(node,width) => node.scrollWidth>node.clientWidth && node.getBoundingClientRect().right<=width", long_workspace[2])


def test_focusing_the_phone_composer_preserves_the_visual_viewport(long_workspace):
    page = long_workspace[0]
    page.locator("#message-input").focus()
    assert page.evaluate("width => innerWidth<=width && visualViewport.width<=width && visualViewport.scale===1 && visualViewport.offsetLeft===0", long_workspace[2])
