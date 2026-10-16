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
    """The browser uses the actual app; only cloud credentials and jobs are synthetic."""
    # API fixtures must remain visible to Playwright rather than a service worker.
    public_shell_test = request.node.name in {
        "test_service_worker_does_not_cache_private_api_responses", "test_an_offline_phone_sees_a_reconnect_page"
    }
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                                  service_workers="allow" if public_shell_test else "block")
    page = context.new_page()
    state = {"authenticated": False, "pairing": [], "jobs": [], "pools": [], "posts": [],
             "profiles": {"default": {"label": "Project tools"}}}
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
        if route.request.method == "POST":
            request = route.request.post_data_json
            state["posts"].append(request)
            if request["action"] == "spawn":
                job = {"id": request["id"], "status": "queued", "spec": request["spec"]}
                state["jobs"].append(job)
            else:
                job = next(job for job in state["jobs"] if job["id"] == request["id"])
                job["status"] = "queued" if request["action"] in ("recheck", "reconcile") else "stopped"
            route.fulfill(json=job)
        else:
            route.fulfill(json={"jobs": state["jobs"], "pools": state["pools"], "profiles": state["profiles"]})

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


def test_unknown_profile_approval_settings_use_neutral_disclosure(paired):
    page, _state = paired
    page.locator("#staff-team").click()
    assert page.locator("#staff-access-disclosure").inner_text() == "This teammate uses this profile’s repository access, credentials and approval settings."


@pytest.mark.parametrize(("harness", "args"), [("codex", ["--dangerously-bypass-approvals-and-sandbox"]),
                                               ("claude", ["--dangerously-skip-permissions"]),
                                               ("omp", ["--approval-mode=yolo"]), ("omp", ["--approval-mode", "yolo"])])
def test_explicit_unattended_profile_is_disclosed_for_selected_harness(paired, harness, args):
    page, state = paired
    state["profiles"] = {"default": {"harness_args": {harness: args}}}
    page.locator("#staff-team").click()
    page.locator("#staff-profile option").filter(has_text="default").wait_for(state="attached")
    page.locator("#staff-harness").select_option(harness)
    page.locator("#staff-access-disclosure").filter(has_text="runs unattended").wait_for()
    assert page.locator("#staff-access-disclosure").inner_text() == "This teammate runs unattended with this profile’s repository access and credentials."


def test_disclosure_changes_with_the_selected_profile(paired):
    page, state = paired
    state["profiles"] = {"default": {"harness_args": {"codex": ["--dangerously-bypass-approvals-and-sandbox"]}},
                         "custom": {"harness_args": {"codex": []}}}
    page.locator("#staff-team").click()
    page.locator("#staff-profile option").filter(has_text="custom").wait_for(state="attached")
    page.locator("#staff-profile").select_option("custom")
    assert "runs unattended" not in page.locator("#staff-access-disclosure").inner_text()


def test_disclosure_changes_with_the_selected_harness(paired):
    page, state = paired
    state["profiles"] = {"default": {"harness_args": {"codex": ["--dangerously-bypass-approvals-and-sandbox"], "claude": []}}}
    page.locator("#staff-team").click()
    page.locator("#staff-access-disclosure").filter(has_text="runs unattended").wait_for()
    page.locator("#staff-harness").select_option("claude")
    assert "runs unattended" not in page.locator("#staff-access-disclosure").inner_text()


@pytest.fixture
def submitted(paired):
    page, state = paired
    page.locator("#staff-team").click()
    page.locator("#staff-name").fill("jack")
    page.locator("#staff-prompt").fill("Review the current implementation.")
    page.locator("#staff-submit").click()
    page.locator(".worker-job").wait_for()
    return page, state


def test_staffing_request_preserves_human_intent(submitted):
    assert submitted[1]["posts"][0]["spec"]["prompt"] == "Review the current implementation."


def test_staffing_request_does_not_choose_a_sender(submitted):
    assert set(submitted[1]["posts"][0]) == {"action", "id", "spec"}


def test_phone_sees_job_preparation_status(submitted):
    assert "Getting started" in submitted[0].locator(".worker-job").inner_text()


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
    page.locator(".worker-job").wait_for()
    assert attempts[0]["id"] == attempts[1]["id"]


@pytest.fixture
def allocation_blocked(paired):
    page, state = paired
    state["jobs"] = [{"id": "blocked-job", "status": "unconfirmed", "phase": "allocation-blocked",
                      "box_id": None, "pool_id": None, "blocked_by_pool": "unresolved-pool",
                      "spec": {"name": "jack"}, "error": "Reconcile the earlier box creation before rechecking."}]
    page.locator("#staff-team").click()
    page.locator(".worker-job").filter(has_text="Waiting for box recovery").wait_for()
    return page, state


def test_allocation_blocked_job_can_recheck_the_existing_request(allocation_blocked):
    page, state = allocation_blocked
    with page.expect_response(lambda response: response.url.endswith("/api/providers/workers") and response.request.method == "POST") as response:
        page.get_by_role("button", name="Check again", exact=True).click()
    response.value.finished()
    assert state["posts"] == [{"action": "recheck", "id": "blocked-job"}]


def test_allocation_blocked_job_can_cancel_the_unstarted_request(allocation_blocked):
    page, state = allocation_blocked
    with page.expect_response(lambda response: response.url.endswith("/api/providers/workers") and response.request.method == "POST") as response:
        page.get_by_role("button", name="Cancel request", exact=True).click()
    response.value.finished()
    assert state["posts"] == [{"action": "stop", "id": "blocked-job"}]


def test_allocation_blocked_job_does_not_look_online(allocation_blocked):
    assert allocation_blocked[0].get_by_role("button", name="Message", exact=True).count() == 0


@pytest.fixture
def orphaned_allocation(paired):
    page, state = paired
    state["jobs"] = [{"id": "orphaned-job", "status": "unconfirmed", "phase": "allocation-blocked",
                      "box_id": None, "pool_id": None, "blocked_by_pool": "unresolved-pool",
                      "spec": {"name": "jack", "profile": "default"}}]
    state["pools"] = [{"id": "unresolved-pool", "box_id": None, "status": "unconfirmed", "profile": "default"}]
    page.locator("#staff-team").click()
    page.get_by_label("Reconcile box for jack", exact=True).wait_for()
    return page, state


def test_orphaned_allocation_can_attach_only_the_explicit_existing_box(orphaned_allocation):
    page, state = orphaned_allocation
    page.get_by_label("Reconcile box for jack", exact=True).fill("bx_inspected")
    with page.expect_response(lambda response: response.url.endswith("/api/providers/workers") and response.request.method == "POST") as response:
        page.get_by_role("button", name="Use this existing box", exact=True).click()
    response.value.finished()
    assert state["posts"] == [{"action": "reconcile", "id": "orphaned-job", "box_id": "bx_inspected"}]


@pytest.mark.parametrize("owner_status", ["unconfirmed", "failed"])
def test_blocked_sibling_cannot_reconcile_while_original_owner_is_recoverable(orphaned_allocation, owner_status):
    page, state = orphaned_allocation
    page.get_by_role("button", name="Close add teammate", exact=True).click()
    state["jobs"].append({"id": "original-owner", "pool_id": "unresolved-pool", "status": owner_status,
                          "phase": "creating", "spec": {"name": "original", "profile": "default"}})
    page.locator("#staff-team").click()
    page.get_by_label("Reconcile box for jack", exact=True).wait_for(state="detached")
    assert page.get_by_label("Reconcile box for jack", exact=True).count() == 0


@pytest.mark.parametrize(("field", "value"), [("box_id", "bx_tracked"), ("profile", "other-profile"), ("status", "retired")])
def test_blocked_reconciliation_requires_a_matching_boxless_pool(orphaned_allocation, field, value):
    page, state = orphaned_allocation
    page.get_by_role("button", name="Close add teammate", exact=True).click()
    state["pools"][0][field] = value
    page.locator("#staff-team").click()
    page.get_by_label("Reconcile box for jack", exact=True).wait_for(state="detached")
    assert page.get_by_label("Reconcile box for jack", exact=True).count() == 0


def test_boxless_reservation_has_no_box_power_action(orphaned_allocation):
    assert orphaned_allocation[0].locator(".pool-job button").count() == 0


def test_boxless_reservation_explains_its_unresolved_state(orphaned_allocation):
    assert "Box creation needs checking" in orphaned_allocation[0].locator(".pool-job").inner_text()


@pytest.mark.parametrize("pool_status", ["stopped", "unconfirmed", None])
def test_box_stopped_job_requires_an_available_associated_pool(paired, pool_status):
    page, state = paired
    state["jobs"] = [{"id": "stopped-job", "status": "stopped", "phase": "pool-stopped",
                      "pool_id": "pool", "box_id": "bx_existing", "spec": {"name": "jack"}}]
    state["pools"] = [{"id": "pool", "status": pool_status}] if pool_status else []
    page.locator("#staff-team").click()
    page.locator(".worker-job").filter(has_text="Resume its box").wait_for()
    assert page.get_by_role("button", name="Check again", exact=True).count() == 0
