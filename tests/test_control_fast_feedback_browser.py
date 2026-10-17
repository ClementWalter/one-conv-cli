"""Sending from the phone: instant local feedback, honest receipts, and change-driven replies over real HTTPS."""

import time

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = browser_tests.browser

# Fills the composer and submits it inside the page, timing the local bubble and the durable receipt.
MEASURE = """async (text) => {
  const input = document.getElementById("message-input");
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  const appeared = (test) => new Promise((resolve) => {
    const check = () => { if (test()) { observer.disconnect(); resolve(performance.now()); return true; } return false; };
    const observer = new MutationObserver(check);
    observer.observe(document.getElementById("messages"), { childList: true, subtree: true, characterData: true });
    check();
  });
  const bubble = () => [...document.querySelectorAll("#messages li")].find((li) => li.textContent.includes(text));
  const started = performance.now();
  const local = appeared(() => bubble());
  const saved = appeared(() => { const li = bubble(); return li && /Saved|Accepted|awaiting/.test(li.textContent); });
  document.getElementById("composer").requestSubmit();
  return { local: (await local) - started, saved: (await saved) - started };
}"""


@pytest.fixture(params=[320, 390])
def phone(browser, real_http_workspace, request):
    workspace = real_http_workspace
    # Requests answered by the offline service worker would bypass the network stubs below.
    context = browser.new_context(viewport={"width": request.param, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block")
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    # The change feed keeps one request open, so "network idle" never comes; wait for the composer instead.
    page.goto(workspace["origin"] + "/#topic=release")
    page.reload()
    page.locator("#composer:not([hidden]) #message-input").wait_for()
    yield page, workspace
    context.close()


def _slow(page, seconds):
    def handle(route):
        time.sleep(seconds)
        route.continue_()
    page.route("**/api/messages", handle)


@pytest.fixture
def slow_send(phone):
    page, workspace = phone
    _slow(page, 1.5)
    timings = page.evaluate(MEASURE, "Ship it on a slow network.")
    return page, workspace, timings


def test_the_local_bubble_paints_within_100ms(slow_send):
    assert slow_send[2]["local"] < 100


def test_the_durable_receipt_waits_for_the_server(slow_send):
    assert slow_send[2]["saved"] >= 1400


def test_the_bubble_says_sending_before_the_server_answers(phone):
    page, _ = phone
    _slow(page, 1.5)
    page.locator("#message-input").fill("Still on its way.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    assert page.locator("#messages li").filter(has_text="Still on its way.").locator(".message-delivery") \
        .inner_text(timeout=500) == "Sending…"


def test_a_slow_send_is_stored_once(slow_send):
    _, workspace, _ = slow_send
    messages = workspace["browser"].get("/api/topics/release").json()["messages"]
    assert [m["text"] for m in messages] == ["Ship it on a slow network."]


def test_a_slow_send_shows_one_bubble(slow_send):
    page, _, _ = slow_send
    page.wait_for_timeout(500)
    assert page.locator("#messages li").filter(has_text="Ship it on a slow network.").count() == 1


# --- a response lost after the server stored the message ---------------------------------------------


@pytest.fixture
def lost_answer(phone):
    page, workspace = phone

    def commit_then_drop(route):
        route.fetch()  # The workspace stores the message …
        route.abort()  # … but the phone never hears back.
    page.route("**/api/messages", commit_then_drop)
    page.locator("#message-input").fill("Did this arrive?")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    status = page.locator("#messages li").filter(has_text="Did this arrive?").locator(".message-delivery")
    status.filter(has_text="Not confirmed").wait_for()
    return page, workspace, status


def test_a_lost_answer_is_not_claimed_as_not_sent(lost_answer):
    assert "Not sent" not in lost_answer[2].inner_text()


def test_a_lost_answer_offers_a_retry(lost_answer):
    assert lost_answer[2].get_by_role("button", name="Retry").count() == 1


@pytest.fixture
def retried(lost_answer):
    page, workspace, status = lost_answer
    page.unroute("**/api/messages")
    status.get_by_role("button", name="Retry").click()
    page.locator("#messages li").filter(has_text="Did this arrive?").filter(has_text="Saved").first.wait_for()
    page.wait_for_timeout(500)
    return page, workspace


def test_retry_after_a_lost_answer_keeps_one_hub_post(retried):
    messages = retried[1]["browser"].get("/api/topics/release").json()["messages"]
    assert [m["text"] for m in messages] == ["Did this arrive?"]


def test_retry_after_a_lost_answer_keeps_one_bubble(retried):
    assert retried[0].locator("#messages li").filter(has_text="Did this arrive?").count() == 1


# --- switching conversation and newer drafts during a send -------------------------------------------------


@pytest.fixture
def switched(phone):
    page, workspace = phone
    _slow(page, 1.5)
    page.locator("#message-input").fill("Only for release.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.evaluate("location.hash = 'topic=general'")
    page.locator("#message-input").wait_for()
    return page, workspace


def test_a_send_does_not_follow_you_to_another_channel(switched):
    page, _ = switched
    page.wait_for_timeout(2000)
    assert page.locator("#messages li").filter(has_text="Only for release.").count() == 0


def test_a_send_stays_in_its_own_channel(switched):
    page, _ = switched
    page.wait_for_timeout(2000)
    page.evaluate("location.hash = 'topic=release'")
    page.locator("#messages li").filter(has_text="Only for release.").wait_for()
    assert page.locator("#messages li").filter(has_text="Only for release.").count() == 1


def test_a_late_answer_keeps_a_newer_draft(phone):
    page, _ = phone
    _slow(page, 1.5)
    page.locator("#message-input").fill("First message.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.locator("#message-input").fill("A newer draft.")
    page.locator("#messages li").filter(has_text="First message.").filter(has_text="Saved").wait_for()
    assert page.locator("#message-input").input_value() == "A newer draft."


# --- replies arrive through the change feed -----------------------------------------------------------------


@pytest.fixture
def replied(phone):
    page, workspace = phone
    workspace["worker"].put("/v1/topics/release/members/omp:agent").raise_for_status()
    page.locator("#message-input").fill("Status?")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.locator("#messages li").filter(has_text="Status?").filter(has_text="Saved").first.wait_for()
    page.wait_for_timeout(1000)  # Let the feed settle into a wait.
    started = time.monotonic()
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "#release",
                                                  "text": "All green."}).raise_for_status()
    page.locator(".message-body").filter(has_text="All green.").wait_for(timeout=10000)
    return time.monotonic() - started


def test_a_reply_appears_without_waiting_for_the_refresh_timers(replied):
    assert replied < 2.0


# --- receipts that do not prove a commit, and commits seen before the answer ----------------------------


def test_an_empty_success_answer_is_not_a_receipt(phone):
    page, _ = phone
    page.route("**/api/messages", lambda route: route.fulfill(status=200, content_type="application/json", body="{}"))
    page.locator("#message-input").fill("Unproven.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    status = page.locator("#messages li").filter(has_text="Unproven.").locator(".message-delivery")
    status.filter(has_text="Not confirmed").wait_for()
    assert status.filter(has_text="Saved").count() == 0


@pytest.fixture
def seen_first(phone):
    """The feed shows the committed post before the POST answer, which then fails."""
    page, workspace = phone

    def commit_then_fail_late(route):
        route.fetch()
        time.sleep(2.5)  # Long enough for the change feed to show the stored post.
        route.fulfill(status=503, content_type="application/json", body='{"error": "late failure"}')
    page.route("**/api/messages", commit_then_fail_late, times=1)
    page.locator("#message-input").fill("Same words.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.wait_for_timeout(4000)
    return page, workspace


def test_a_late_failure_does_not_downgrade_a_stored_post(seen_first):
    assert seen_first[0].locator("#messages li").filter(has_text="Same words.").filter(
        has_text="Not confirmed").count() == 0


@pytest.fixture
def sent_again(seen_first):
    page, workspace = seen_first
    page.locator("#message-input").fill("Same words.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.wait_for_function("document.querySelectorAll('#messages li').length >= 2 && "
                           "[...document.querySelectorAll('#messages li')].filter(li => "
                           "li.textContent.includes('Same words.')).length === 2", timeout=10000)
    return workspace


def test_the_same_words_sent_again_are_a_new_post(sent_again):
    messages = sent_again["browser"].get("/api/topics/release").json()["messages"]
    assert [m["text"] for m in messages] == ["Same words.", "Same words."]


# --- session isolation --------------------------------------------------------------------------------


@pytest.fixture
def repaired(phone):
    """Disconnect while the feed waits, then pair again in the same tab."""
    page, workspace = phone
    page.wait_for_timeout(1000)  # The feed is now waiting on the old session.
    page.get_by_role("button", name="Device settings").click()
    page.get_by_role("button", name="Disconnect this device").click()
    page.locator("#pairing").wait_for()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token)
    page.reload()
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    page.evaluate("location.hash = 'topic=release'")
    page.locator("#composer:not([hidden]) #message-input").wait_for()
    page.wait_for_timeout(2000)  # Any stale answer from the old session has arrived by now.
    return page, workspace


def test_a_stale_answer_does_not_disconnect_the_new_session(repaired):
    assert repaired[0].locator("#pairing").is_hidden()


def test_the_new_session_gets_its_own_change_feed(repaired):
    page, workspace = repaired
    workspace["worker"].put("/v1/topics/release/members/omp:agent").raise_for_status()
    workspace["browser"].post("/api/messages", json={"id": "e" * 32, "target": "#release", "text": "Kickoff."},
                              headers=workspace["headers"]).raise_for_status()
    page.wait_for_timeout(1000)
    started = time.monotonic()
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "#release",
                                                  "text": "Back online."}).raise_for_status()
    page.locator(".message-body").filter(has_text="Back online.").wait_for(timeout=10000)
    assert time.monotonic() - started < 2.0
