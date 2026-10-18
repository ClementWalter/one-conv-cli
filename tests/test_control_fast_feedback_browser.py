"""Sending from the phone: instant local feedback, honest receipts, and change-driven replies over real HTTPS."""

import time

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as browser_tests

PlaywrightError = pytest.importorskip("playwright.sync_api").Error

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
    # A full navigation to the channel, so no refresh left over from pairing can pick the view; a
    # hash change plus reload could race it under load.
    page.goto("about:blank")
    page.goto(workspace["origin"] + "/#topic=release")
    # The destination is a checked precondition: a navigation problem fails here, not as a message
    # stored in another channel. The change feed keeps a request open, so "network idle" never comes.
    page.wait_for_function("() => location.hash === '#topic=release' && typeof model !== 'undefined'"
                           " && model.view && model.view.kind === 'topic' && model.view.key === 'release'")
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
    # Freeze what could show the stored post: the change feed, and the channel snapshot the safety
    # refresh reads. Once the post is stored, either would rightly replace the uncertain bubble (and
    # its Retry button) before these tests can read it. The uncertain state itself is local.
    held = []
    page.route("**/api/changes*", lambda route: held.append(route))
    page.route("**/api/topics/release", lambda route: held.append(route))
    page.evaluate("() => model.feedAbort && model.feedAbort.abort()")
    page.locator("#message-input").fill("Did this arrive?")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    status = page.locator("#messages li").filter(has_text="Did this arrive?").locator(".message-delivery")
    status.filter(has_text="Not confirmed").wait_for()
    return page, workspace, status, held


def test_a_lost_answer_is_not_claimed_as_not_sent(lost_answer):
    assert "Not sent" not in lost_answer[2].inner_text()


def test_a_lost_answer_offers_a_retry(lost_answer):
    assert lost_answer[2].get_by_role("button", name="Retry").count() == 1


@pytest.fixture
def retried(lost_answer):
    page, workspace, status, held = lost_answer
    page.unroute("**/api/messages")
    status.get_by_role("button", name="Retry").click()
    page.locator("#messages li").filter(has_text="Did this arrive?").filter(has_text="Saved").first.wait_for()
    # The stored copy reaches the phone again, so one bubble means the retry and the stored post merged.
    page.unroute("**/api/changes*")
    page.unroute("**/api/topics/release")
    for route in held:
        try:
            route.continue_()
        except PlaywrightError:
            pass  # The page already cancelled this read (the aborted feed); there is nothing to release.
    with page.expect_response("**/api/topics/release"):
        page.evaluate("() => refreshState()")
    # The server's row has replaced this tab's local copy of the send.
    page.locator("#messages li").filter(has_text="Did this arrive?").locator(".message-delivery[class*='local-']") \
        .wait_for(state="detached")
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


def owned_request(workspace) -> dict:
    """The agent claims the open human request in #release, as the one agent that answers it."""
    open_tasks = workspace["worker"].get("/v1/tasks", params={"topic": "release", "open": "1"}).json()["tasks"]
    claimed = workspace["worker"].post("/v1/tasks/claim", json={"key": open_tasks[0]["key"], "actor": "omp:agent"})
    claimed.raise_for_status()
    task = claimed.json()["task"]
    return {"key": task["key"], "version": task["version"]}


@pytest.fixture
def replied(phone):
    page, workspace = phone
    workspace["worker"].put("/v1/topics/release/members/omp:agent").raise_for_status()
    page.locator("#message-input").fill("Status?")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.locator("#messages li").filter(has_text="Status?").filter(has_text="Saved").first.wait_for()
    link = owned_request(workspace)
    page.wait_for_timeout(1000)  # Let the feed settle into a wait.
    started = time.monotonic()
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "#release",
                                                  "text": "All green.", "task": link}).raise_for_status()
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

    # The post is stored at once; its answer is replaced by a failure 2.5 s later, inside the page.
    page.evaluate("""() => {
      const original = window.fetch;
      let first = true;
      window.fetch = async (url, options) => {
        const response = await original(url, options);
        if (!first || !String(url).includes('/api/messages')) return response;
        first = false;
        await new Promise((resolve) => setTimeout(resolve, 2500));
        return new Response('{"error": "late failure"}', { status: 503, headers: { 'Content-Type': 'application/json' } });
      };
    }""")
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
    # A second bubble can be the optimistic one; wait for the server's answer before reading storage.
    with page.expect_response("**/api/messages") as answer:
        page.locator("#composer").evaluate("form => form.requestSubmit()")
    answer.value.finished()
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
    link = owned_request(workspace)
    page.wait_for_timeout(1000)
    started = time.monotonic()
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "#release",
                                                  "text": "Back online.", "task": link}).raise_for_status()
    page.locator(".message-body").filter(has_text="Back online.").wait_for(timeout=10000)
    assert time.monotonic() - started < 2.0


# --- review regressions: races inside one open page ------------------------------------------------------


# Delays answers inside the page: Playwright routes would queue every later matching request behind a held one.
HOLD_FIRST = """(pattern) => {
  const original = window.fetch;
  let held = true;
  window.__release = null;
  const gate = new Promise((resolve) => { window.__release = resolve; });
  window.fetch = async (url, options) => {
    if (held && String(url).includes(pattern)) { held = false; await gate; }
    return original(url, options);
  };
}"""


@pytest.fixture
def held_first_feed(browser, real_http_workspace):
    """The first change-feed request leaves only after an agent's reply is committed."""
    workspace = real_http_workspace
    workspace["worker"].put("/v1/topics/release/members/omp:agent").raise_for_status()
    context = browser.new_context(viewport={"width": 390, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block")
    context.add_init_script(f"({HOLD_FIRST})('/api/changes')")
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#topic=release")
    page.evaluate("""async (token) => {
      await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify({ token }) });
    }""", token)
    page.reload()
    page.locator("#composer:not([hidden]) #message-input").wait_for()
    page.wait_for_timeout(1000)  # The initial snapshot, without the reply, is on screen.
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "#release",
                                                  "text": "Committed while the feed was held."}).raise_for_status()
    page.evaluate("window.__release()")
    released = time.monotonic()
    page.locator(".message-body").filter(has_text="Committed while the feed was held.").wait_for(timeout=40000)
    yield time.monotonic() - released
    context.close()


def test_a_reply_committed_while_the_first_feed_was_held_appears_promptly(held_first_feed):
    assert held_first_feed < 3


@pytest.mark.parametrize("answer", [
    lambda i: {"id": i, "status": "refused", "receipt": {"id": i}},
    lambda i: {"id": i, "status": "sent"},
    lambda i: {"id": i, "status": "sent", "receipt": {"id": i, "from": "user", "to": "#other", "status": "stored"}},
    lambda i: {"id": i, "status": "sent", "receipt": {"id": i, "from": "codex:x", "to": "#release", "status": "s"}},
    lambda i: {"id": i, "status": "sent", "receipt": {"id": i, "from": "user", "to": "#release", "status": 1}},
    lambda i: {"id": i, "status": "sent", "receipt": {"id": i, "from": "user", "to": "#release", "status": "s",
                                                      "deliveries": [{"to": 1}]}},
    lambda i: [],
    lambda i: None,
])
def test_a_receipt_that_does_not_prove_storage_is_not_saved(phone, answer):
    page, _ = phone
    import json as _json
    page.route("**/api/messages", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=_json.dumps(answer(route.request.post_data_json["id"]))))
    page.locator("#message-input").fill("Unproven receipt.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.locator("#messages li").filter(has_text="Unproven receipt.").locator(".message-delivery") \
        .filter(has_text="Not confirmed").wait_for()
    assert page.locator("#messages li").filter(has_text="Unproven receipt.").filter(has_text="Saved").count() == 0


DELAY_FIRST_ANSWER = """([pattern, ms]) => {
  const original = window.fetch;
  let first = true;
  window.fetch = async (url, options) => {
    const response = await original(url, options);
    if (!first || !String(url).includes(pattern)) return response;
    first = false;
    const body = await response.text();
    await new Promise((resolve) => setTimeout(resolve, ms));
    return new Response(body, { status: response.status, headers: response.headers });
  };
}"""


@pytest.fixture
def stale_view_read(phone):
    """An old read of #beta, taken before the send, arrives after a newer read that shows it."""
    page, _ = phone
    page.evaluate(DELAY_FIRST_ANSWER, ["/api/topics/beta", 3000])
    page.evaluate("""() => {
      location.hash = 'topic=beta';
      setTimeout(() => {
        const input = document.getElementById('message-input');
        input.value = 'Kept after a stale read.';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('composer').requestSubmit();
      }, 800);
    }""")
    page.wait_for_timeout(5000)
    return page


def test_an_older_read_of_the_same_view_does_not_erase_a_stored_send(stale_view_read):
    assert stale_view_read.locator("#messages li").filter(has_text="Kept after a stale read.").count() == 1


@pytest.fixture
def stale_unauthorized(phone):
    """A read from the old session answers 401 after the same page has signed in again."""
    page, workspace = phone
    token = AuthStore(workspace["directory"]).issue_pairing_token()

    def late_unauthorized(route):
        time.sleep(3)
        route.fulfill(status=401, content_type="application/json", body='{"error": "Pair this browser."}')

    page.route("**/api/topics/release", late_unauthorized, times=1)
    page.evaluate("""async (token) => {
      refreshView();
      await new Promise((resolve) => setTimeout(resolve, 300));
      await disconnectDevice();
      await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify({ token }) });
      if (await session()) { refreshState(); changeFeed(); }
    }""", token)
    page.wait_for_timeout(4000)
    return page


def test_a_stale_unauthorized_read_does_not_sign_out_the_new_session(stale_unauthorized):
    assert stale_unauthorized.evaluate("model.authenticated") is True


@pytest.mark.parametrize("body", ["{}", '{"fp": "abc", "changed": true}', '{"fp": "' + "a" * 32 + '", "changed": "yes"}',
                                  "[]", "null"])
def test_a_malformed_feed_answer_backs_off(phone, body):
    page, _ = phone
    calls = []

    def malformed(route):
        calls.append(time.monotonic())
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/changes*", malformed)
    page.evaluate("() => { if (model.feedAbort) model.feedAbort.abort(); }")
    page.wait_for_timeout(3000)
    assert len(calls) <= 4


def test_sending_feedback_stays_with_its_conversation(phone):
    page, _ = phone
    _slow(page, 2)
    page.locator("#message-input").fill("For release only.")
    page.locator("#composer").evaluate("form => form.requestSubmit()")
    page.evaluate("location.hash = 'topic=beta'")
    page.locator("#message-input").fill("An unsent draft for beta.")
    page.wait_for_timeout(2500)
    assert "Sending" not in page.locator("#composer-feedback").inner_text()


@pytest.fixture
def repaired_behind_a_slow_refresh(phone):
    """The old session's state read is still in flight when the same page signs in again."""
    page, workspace = phone
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.evaluate(DELAY_FIRST_ANSWER, ["/api/state", 6000])
    page.evaluate("""async (token) => {
      refreshState();
      await new Promise((resolve) => setTimeout(resolve, 300));
      await disconnectDevice();
      await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                  body: JSON.stringify({ token }) });
      if (await session()) refreshState();
    }""", token)
    page.wait_for_timeout(2500)
    return page


def test_a_new_session_does_not_wait_behind_the_old_sessions_refresh(repaired_behind_a_slow_refresh):
    assert repaired_behind_a_slow_refresh.evaluate("model.state.topics.length") >= 1


LOSE_ANSWERS = """() => {
  const original = window.fetch;
  window.fetch = async (url, options) => {
    const response = await original(url, options);
    if (String(url).includes('/api/messages')) throw new TypeError('connection lost after commit');
    return response;
  };
}"""


@pytest.fixture
def two_lost_sends(phone):
    """Lost answers in #alpha then #beta; returning to #alpha once its stored copy is visible."""
    page, _ = phone
    page.evaluate(LOSE_ANSWERS)
    # Each chat is left the moment its send turns uncertain, before a refresh can show the stored copy.
    for topic, text, leave in (("alpha", "Alpha task.", "beta"), ("beta", "Beta task.", "alpha")):
        page.evaluate(f"location.hash = 'topic={topic}'")
        page.locator("#composer:not([hidden]) #message-input").wait_for()
        page.evaluate("""([text, leave]) => new Promise((resolve) => {
          const observer = new MutationObserver(() => {
            if ([...document.querySelectorAll('#messages li')].some((li) =>
                li.textContent.includes(text) && li.textContent.includes('Not confirmed'))) {
              observer.disconnect();
              location.hash = 'topic=' + leave;
              resolve();
            }
          });
          observer.observe(document.getElementById('messages'), { childList: true, subtree: true });
          const input = document.getElementById('message-input');
          input.value = text;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          document.getElementById('composer').requestSubmit();
        })""", [text, leave])
    page.locator(".message-body").filter(has_text="Alpha task.").first.wait_for()
    page.wait_for_timeout(1500)
    return page


def test_a_restored_draft_is_cleared_in_its_own_chat_after_another_chat_lost_an_answer(two_lost_sends):
    assert two_lost_sends.locator("#message-input").input_value() == ""
