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
"""Phone interactions exercise production assets with explicit cloud API fixtures."""

import os
from urllib.parse import urlsplit

import pytest

from tests import test_live_web_browser as workspace_tests

workspace = workspace_tests.workspace


@pytest.fixture(scope="module")
def browser():
    """WebKit and Chromium exercise the same phone interactions and public shell."""
    with workspace_tests.playwright.sync_playwright() as runtime:
        engine = os.getenv("ONE_CONV_TEST_ENGINE", "chromium")
        options = {"headless": True}
        if engine == "chromium" and os.getenv("ONE_CONV_TEST_BROWSER"):
            options["executable_path"] = os.environ["ONE_CONV_TEST_BROWSER"]
        instance = getattr(runtime, engine).launch(**options)
        yield instance
        instance.close()


@pytest.fixture
def cloud_page(browser, workspace, request):
    """The browser uses the actual app; only the workspace's answers are synthetic."""
    # API fixtures must remain visible to Playwright rather than a service worker.
    public_shell_test = request.node.name in {
        "test_service_worker_does_not_cache_private_api_responses", "test_an_offline_phone_sees_a_reconnect_page"
    }
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                  service_workers="allow" if public_shell_test else "block")
    page = context.new_page()
    state = {"authenticated": False, "pairing": [], "posts": []}
    page.on("pageerror", lambda error: state.setdefault("errors", []).append(str(error)))
    page.on("console", lambda event: state.setdefault("console", []).append(event.text) if event.type == "error" else None)
    page.on("requestfailed", lambda event: state.setdefault("failures", []).append(event.failure))

    def session(route):
        data = {"authenticated": False, "human": None}
        if state["authenticated"]:
            data = {"authenticated": True, "csrf_token": "synthetic-csrf", "human": {"address": "user", "name": "You"},
                    "capabilities": {"workers": True, "accounting": True}}
        route.fulfill(json=data)

    def login(route):
        state["pairing"].append(route.request.post_data_json)
        state["authenticated"] = True
        route.fulfill(json={"authenticated": True})

    def workers(route):
        request = route.request.post_data_json
        state["posts"].append(request)
        route.fulfill(json={"id": request["id"], "status": "node", "route": "macbook/spawn:claude"})

    def accounting(route):
        if route.request.method == "POST":
            route.fulfill(json={"expected_cost": {"amount": None, "currency": "USD"}, "hourly_rate": {"basis": "allocated"}})
        else:
            route.fulfill(json={"cards": [{"cost_center_id": "jack", "name": "Jack", "hourly_rate": {
                "amount": None, "currency": "USD", "basis": "allocated", "metering_basis": "Occupied work hour"}}], "events": []})

    page.route("**/api/session", session)
    page.route("**/api/login", login)
    page.route("**/api/logout", lambda route: route.fulfill(json={"authenticated": False}))
    page.route("**/api/providers/workers", workers)
    page.route("**/api/providers/accounting", accounting)
    page.goto(workspace["url"] + "/#pair=synthetic-pairing-token", wait_until="networkidle")
    yield page, state
    context.close()


@pytest.fixture
def paired(cloud_page):
    page, state = cloud_page
    page.get_by_role("button", name="Connect this device", exact=True).click()
    try:
        page.locator("#title").filter(has_text="design-review").wait_for(timeout=5000)
    except workspace_tests.playwright.TimeoutError as error:
        raise RuntimeError({"state": state, "title": page.locator("#title").inner_text(),
                            "pairing": page.locator("#pairing").inner_text()}) from error
    return page, state


def test_pairing_secret_is_removed_from_the_location(cloud_page):
    assert urlsplit(cloud_page[0].url).fragment == ""


def test_locked_device_does_not_offer_the_conversation(cloud_page):
    assert cloud_page[0].locator(".workspace").is_hidden()


def test_pairing_requires_an_explicit_click(cloud_page):
    assert cloud_page[1]["pairing"] == []


def test_pairing_posts_only_the_invitation(paired):
    assert paired[1]["pairing"] == [{"token": "synthetic-pairing-token"}]


def test_phone_can_open_the_staffing_form(paired):
    page = paired[0]
    page.locator("#staff-team").click()
    assert page.locator("#staff-dialog").is_visible()


@pytest.fixture
def submitted(paired):
    page, state = paired
    page.locator("#staff-team").click()
    page.locator("#staff-name").fill("jack")
    page.locator("#staff-prompt").fill("Review the current implementation.")
    page.locator("#staff-submit").click()
    page.locator("#staff-feedback").filter(has_text="Starting on").wait_for()
    return page, state


def test_staffing_request_preserves_human_intent(submitted):
    assert submitted[1]["posts"][0]["spec"]["prompt"] == "Review the current implementation."


def test_staffing_request_does_not_choose_a_sender(submitted):
    assert set(submitted[1]["posts"][0]) == {"action", "id", "spec"}


def test_phone_sees_which_machine_starts_the_teammate(submitted):
    assert "Starting on macbook" in submitted[0].locator("#staff-feedback").inner_text()


def test_unknown_rate_is_not_displayed_as_zero(paired):
    page = paired[0]
    page.get_by_role("button", name="Team costs").click()
    page.locator(".rate-card").wait_for()
    assert page.locator(".rate-amount").inner_text() == "Not measured"


def test_unknown_quote_remains_unknown_in_the_browser(paired):
    page = paired[0]
    page.get_by_role("button", name="Team costs").click()
    page.get_by_role("button", name="Estimate", exact=True).click()
    page.locator(".rate-card > .quiet-copy").last.filter(has_text="Not measured").wait_for()
    assert "Not measured" in page.locator(".rate-card > .quiet-copy").last.inner_text()


def test_phone_layout_has_no_horizontal_overflow(paired):
    assert paired[0].evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.fixture
def usage_costs(paired):
    page = paired[0]
    page.route("**/api/providers/accounting", lambda route: route.fulfill(json={
        "cards": [], "events": [{"cost_center_id": "jack", "model": "example-model",
                                   "timestamp": "2026-10-16T12:00:00Z", "tokens": {"total": 400},
                                   "cash_cost": {"amount": None}, "allocated_cost": {"amount": None},
                                   "estimated_cost": {"amount": "0.000000001", "currency": "USD"}}],
    }))
    page.get_by_role("button", name="Team costs").click()
    page.locator(".usage-row").wait_for()
    return page.locator(".usage-row")


def test_usage_shows_the_answer_token_count(usage_costs):
    assert "400 tokens" in usage_costs.inner_text()


def test_usage_keeps_unknown_cash_separate_from_estimates(usage_costs):
    assert "Cash not reported" in usage_costs.inner_text()


def test_a_tiny_answer_estimate_is_not_rounded_to_zero(usage_costs):
    assert "< 0.00000001 USD estimated" in usage_costs.inner_text()


def test_phone_staffing_form_has_no_horizontal_overflow(paired, tmp_path):
    page = paired[0]
    page.locator("#staff-team").click()
    page.screenshot(path=str(tmp_path / "phone-staffing.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_device_settings_explain_phone_installation(paired):
    page = paired[0]
    page.get_by_role("button", name="Device settings").click()
    assert "Add to Home Screen" in page.locator("#device-dialog").inner_text()


def test_logout_hides_the_workspace(paired):
    page = paired[0]
    page.get_by_role("button", name="Device settings").click()
    page.get_by_role("button", name="Disconnect this device").click()
    page.locator("#pairing").wait_for()
    assert page.locator(".workspace").is_hidden()


def test_app_manifest_is_discoverable(paired):
    assert paired[0].locator('link[rel="manifest"]').get_attribute("href") == "/manifest.webmanifest"


def test_service_worker_does_not_cache_private_api_responses(cloud_page):
    page = cloud_page[0]
    page.evaluate("navigator.serviceWorker.ready")
    paths = page.evaluate("""async () => {
        const result = [];
        for (const name of await caches.keys()) {
            const cache = await caches.open(name);
            for (const request of await cache.keys()) result.push(new URL(request.url).pathname);
        }
        return result;
    }""")
    assert not any(path.startswith(("/api/", "/v1/")) for path in paths)


def test_an_offline_phone_sees_a_reconnect_page(cloud_page):
    page = cloud_page[0]
    page.evaluate("navigator.serviceWorker.ready")
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    assert page.get_by_role("heading", name="Your team is one connection away.").is_visible()


def test_staffing_retry_keeps_its_id_after_a_reload(paired):
    page, _state = paired
    attempts = []

    def ambiguous_response(route):
        if route.request.method == "POST":
            attempts.append(route.request.post_data_json)
            if len(attempts) == 1:
                route.abort()
                return
        route.fallback()

    page.route("**/api/providers/workers", ambiguous_response)
    page.locator("#staff-team").click()
    page.locator("#staff-name").fill("jack")
    page.locator("#staff-prompt").fill("Review the implementation.")
    page.locator("#staff-submit").click()
    page.locator("#staff-feedback").filter(has_text="before a receipt arrived").wait_for()
    page.reload(wait_until="networkidle")
    page.locator("#staff-team").click()
    page.locator("#staff-name").fill("jack")
    page.locator("#staff-prompt").fill("Review the implementation.")
    page.locator("#staff-submit").click()
    page.locator("#staff-feedback").filter(has_text="Starting on").wait_for()
    assert attempts[0]["id"] == attempts[1]["id"]
