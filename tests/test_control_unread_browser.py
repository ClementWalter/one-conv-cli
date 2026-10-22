"""Cloud unread marks over real HTTPS: a channel read on one paired device clears on the other."""

import re

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_live_web_unread_browser as unread_browser_tests

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = unread_browser_tests.browser
width = unread_browser_tests.width
open_sidebar = unread_browser_tests.open_sidebar


def paired(browser, workspace, hash_route, width):
    phone = width < unread_browser_tests.PHONE
    context = browser.new_context(viewport={"width": width, "height": 740 if phone else 900}, is_mobile=phone,
                                  has_touch=phone, ignore_https_errors=True, service_workers="block")
    page = context.new_page()
    token = AuthStore(workspace["directory"]).issue_pairing_token()
    page.goto(workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    # The change feed keeps one request open, so wait for elements rather than network idle.
    page.goto(workspace["origin"] + "/#" + hash_route)
    page.reload()
    page.locator(".conversation-row").first.wait_for(state="attached")
    return context, page


@pytest.fixture
def devices(browser, real_http_workspace, width):
    workspace = real_http_workspace
    worker = workspace["worker"]
    worker.put("/v1/topics/release/members/omp:agent").raise_for_status()
    first_context, first = paired(browser, workspace, "topic=general", width)
    second_context, second = paired(browser, workspace, "topic=general", width)
    worker.post("/v1/messages", json={"id": "e" * 32, "from": "omp:agent", "to": "#release",
                                      "text": "Release notes are ready."}).raise_for_status()
    for page in (first, second):
        page.locator(".conversation-row.unread").filter(has_text="release").wait_for(state="attached", timeout=15000)
    yield first, second
    first_context.close()
    second_context.close()


def test_an_agent_post_is_unread_on_every_device(devices):
    _, second = devices
    assert second.locator(".conversation-row.unread .row-unread").inner_text() == "1"


def test_reading_on_one_device_clears_the_other(devices):
    first, second = devices
    open_sidebar(first)
    first.locator(".conversation-row").filter(has_text="release").click()
    first.locator(".message-body").filter(has_text="Release notes are ready.").wait_for()
    second.locator(".conversation-row.unread").wait_for(state="detached", timeout=15000)
    assert second.locator(".conversation-row.unread").count() == 0


# Holds this tab's first read mark, then answers it with 401 once the page lets it go.
HOLD_READ = """() => {
  const original = window.fetch;
  let held = false;
  window.fetch = async (url, options) => {
    if (!held && String(url).includes("/api/read")) {
      held = true;
      window.__readStarted = true;
      await new Promise((resolve) => { window.__releaseRead = resolve; });
      return new Response(JSON.stringify({ error: "Pair this browser with the workspace." }),
        { status: 401, headers: { "Content-Type": "application/json" } });
    }
    return original(url, options);
  };
}"""

# Disconnects, pairs the same tab again, then releases the old session's read mark.
REPAIR_THEN_RELEASE = """async (token) => {
  await disconnectDevice();
  await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }) });
  if (await session()) { refreshState(); changeFeed(); }
  window.__releaseRead();
}"""


@pytest.fixture
def repaired(browser, real_http_workspace, width):
    """A read mark from the ended session is rejected only after the same tab paired again."""
    workspace = real_http_workspace
    context, page = paired(browser, workspace, "topic=release", width)
    page.evaluate(HOLD_READ)
    workspace["worker"].put("/v1/topics/release/members/omp:agent").raise_for_status()
    workspace["worker"].post("/v1/messages", json={"id": "8" * 32, "from": "omp:agent", "to": "#release",
                                                   "text": "Starts a read mark."}).raise_for_status()
    page.wait_for_function("() => window.__readStarted === true", timeout=15000)
    page.evaluate(REPAIR_THEN_RELEASE, AuthStore(workspace["directory"]).issue_pairing_token())
    page.wait_for_timeout(300)
    yield page
    context.close()


def test_an_old_read_mark_rejection_does_not_sign_out_a_new_pairing(repaired):
    assert repaired.evaluate("model.authenticated") is True


# --- a forwarded Claude Code question ----------------------------------------------------------------

ASK = ("**A question for you** (tap an option, or reply with your own answer)\n\n**Framework**: Which framework?\n"
       "- React\n- Vue\n\n" '<!-- one-conv-ask {"questions": [{"question": "Which framework?", "header": "Framework", '
       '"options": ["React", "Vue"], "multiSelect": false}]} -->')


@pytest.fixture
def question(browser, real_http_workspace):
    real_http_workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "workspace/user",
                                                             "text": ASK}).raise_for_status()
    context, page = paired(browser, real_http_workspace, "direct=user%20worker%2Fomp%3Aagent", 1280)
    page.locator(".ask-option").first.wait_for()
    yield page
    context.close()


def test_a_forwarded_question_offers_its_options_as_buttons(question):
    assert question.locator(".ask-option").all_inner_texts() == ["React", "Vue"]


def test_a_forwarded_question_hides_its_marker(question):
    assert "one-conv-ask" not in question.locator(".message-body").first.inner_text()


def test_tapping_an_option_sends_it_to_the_agent(question):
    question.locator(".ask-option", has_text="Vue").click()
    question.locator(".message-body p").filter(has_text=re.compile(r"^Vue$")).wait_for()
    assert question.locator(".message-body p").filter(has_text=re.compile(r"^Vue$")).count() == 1
