"""Acks on the phone: a check mark per session that read the human's post, at 320 and 390 px."""

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser

POST = "a" * 32
OVERFLOW = "() => document.documentElement.scrollWidth <= window.innerWidth"


@pytest.fixture
def workspace(real_http_workspace):
    """#release has @builder and @reviewer running; the human asked something there."""
    worker = real_http_workspace["worker"]
    worker.put("/v1/sessions", json={"sessions": [
        {"address": "omp:agent", "name": "builder", "status": "idle"},
        {"address": "omp:review", "name": "reviewer", "status": "idle"}]}).raise_for_status()
    for address in ("omp:agent", "omp:review"):
        worker.put(f"/v1/topics/release/members/{address}").raise_for_status()
    real_http_workspace["browser"].post(
        "/api/messages", json={"id": POST, "target": "#release", "text": "Ship the release notes today."},
        headers=real_http_workspace["headers"]).raise_for_status()
    return real_http_workspace


@pytest.fixture(params=[390])
def phone(browser, workspace, request):
    context = browser.new_context(viewport={"width": request.param, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block")
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    page.goto("about:blank")
    page.goto(workspace["origin"] + "/#topic=release")
    page.locator(".message-body").filter(has_text="Ship the release notes today.").wait_for()
    yield page, workspace
    context.close()


@pytest.fixture
def acked(phone):
    page, workspace = phone
    for actor in ("omp:agent", "omp:review"):
        workspace["worker"].post("/v1/acknowledgments", json={"actor": actor, "id": POST}).raise_for_status()
    page.locator(".message-ack").nth(1).wait_for(timeout=15000)
    return page


def test_each_ack_shows_a_check_mark(acked):
    assert acked.locator(".message-ack").all_inner_texts() == ["✓ @builder", "✓ @reviewer"]


def test_the_acks_do_not_overflow(acked):
    assert acked.evaluate(OVERFLOW) is True


def test_the_phone_shows_no_task_chip(acked):
    assert acked.locator(".task-chip").count() == 0
