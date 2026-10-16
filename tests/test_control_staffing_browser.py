"""The phone queues production worker jobs over HTTPS without paid Boat effects."""

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


class BoatWithoutLogin:
    """A disposable provider boundary that creates no remote resources."""

    def credentials(self, _profile):
        return {"service_account": "synthetic-private-service-account"}

    def create(self, _profile, _pool):
        return "bx_synthetic"

    def bootstrap(self, _box, payload):
        return {"status": "stopped"} if payload["operation"] == "stop" else {"status": "auth_needed", "authenticated": False}


@pytest.fixture
def staffed_phone(tmp_path, tls_certificate, browser):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"https://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, origin, worker_config={"boat": BoatWithoutLogin()})
    certificate, key = tls_certificate
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
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
        page.locator("#staff-team").click()
        page.locator("#staff-name").fill("jack")
        page.locator("#staff-topic").fill("release")
        page.locator("#staff-prompt").fill("Review the current change.")
        page.locator("#staff-submit").click()
        page.locator(".worker-job").first.wait_for()
        yield page, app
    finally:
        context.close()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_phone_uses_the_production_profile_mapping(staffed_phone):
    assert staffed_phone[0].locator("#staff-profile").input_value() == "default"


def test_phone_staffing_is_durable_before_remote_completion(staffed_phone):
    assert len(staffed_phone[1].state.workers.manager.list()["jobs"]) == 1


def test_unavailable_provider_does_not_look_online(staffed_phone):
    page, _app = staffed_phone
    page.locator(".worker-job").filter(has_text="Provider needs attention").wait_for(timeout=10000)
    assert page.get_by_role("button", name="Message", exact=True).count() == 0


def test_phone_can_reopen_a_cloud_job_after_reload(staffed_phone):
    page, _app = staffed_phone
    page.reload(wait_until="networkidle")
    page.locator("#staff-team").click()
    page.locator(".worker-job").first.wait_for()
    assert "jack" in page.locator(".worker-job").first.inner_text()


def test_new_worker_rate_is_unknown(staffed_phone):
    assert staffed_phone[1].state.accounting.rates.show("jack")["hourly_rate"]["amount"] is None


def test_phone_can_stop_a_teammate_without_deleting_its_box(staffed_phone):
    page, app = staffed_phone
    page.get_by_role("button", name="Stop teammate").wait_for(timeout=10000)
    page.get_by_role("button", name="Stop teammate").click()
    page.locator(".worker-job").filter(has_text="Stopped").wait_for()
    assert app.state.workers.manager.list()["pools"][0]["box_id"] == "bx_synthetic"


@pytest.mark.parametrize(("entered", "expected"), [("0", "0"), ("12.50", "12.50"), ("", None)])
def test_phone_can_set_an_explicit_planning_rate(staffed_phone, entered, expected):
    page, app = staffed_phone
    page.get_by_role("button", name="Close add teammate").click()
    page.get_by_role("button", name="Team costs").click()
    page.get_by_role("button", name="Set planning rate").click()
    page.get_by_role("spinbutton", name="Hourly rate for jack").fill(entered)
    with page.expect_response("**/api/providers/accounting") as response:
        page.get_by_role("button", name="Save rate", exact=True).click()
    response.value.finished()
    assert app.state.accounting.rates.show("jack")["hourly_rate"]["amount"] == expected


def test_rate_edit_preserves_a_worker_registered_after_the_dialog_opened(staffed_phone):
    page, app = staffed_phone
    page.get_by_role("button", name="Close add teammate").click()
    page.get_by_role("button", name="Team costs").click()
    page.get_by_role("button", name="Set planning rate").wait_for()
    card = app.state.accounting.rates.show("jack")
    card.pop("updated_at")
    card["addresses"] = ["worker-test/codex:late"]
    app.state.accounting.rates.register(card, replace=True)
    page.get_by_role("button", name="Set planning rate").click()
    page.get_by_role("spinbutton", name="Hourly rate for jack").fill("5")
    with page.expect_response("**/api/providers/accounting") as response:
        page.get_by_role("button", name="Save rate", exact=True).click()
    response.value.finished()
    saved = app.state.accounting.rates.show("jack")
    assert (saved["addresses"], saved["hourly_rate"]["amount"]) == (["worker-test/codex:late"], "5")
