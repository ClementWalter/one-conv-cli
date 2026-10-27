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
"""Cloud unread marks over real HTTPS: a channel read on one paired device clears on the other."""

import json
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

def ask_text(*questions):
    marker = json.dumps({"questions": list(questions)})
    return f"**A question for you** (tap an option, or reply with your own answer)\n\n**Details** here\n\n<!-- one-conv-ask {marker} -->"


FRAMEWORK = {"question": "Which framework?", "header": "Framework", "multiSelect": False,
             "options": [{"label": "React", "description": "Most used"}, {"label": "Vue", "description": ""}]}
EXTRAS = {"question": "Which extras?", "header": "Extras", "multiSelect": True,
          "options": [{"label": "Router", "description": ""}, {"label": "Store", "description": ""}]}


def asked(browser, workspace, *questions):
    workspace["worker"].post("/v1/messages", json={"id": "f" * 32, "from": "omp:agent", "to": "workspace/user",
                                                   "text": ask_text(*questions)}).raise_for_status()
    context, page = paired(browser, workspace, "direct=user%20worker%2Fomp%3Aagent", 1280)
    page.locator(".ask-option").first.wait_for()
    return context, page


@pytest.fixture
def question(browser, real_http_workspace):
    context, page = asked(browser, real_http_workspace, FRAMEWORK)
    yield page
    context.close()


@pytest.fixture
def two_questions(browser, real_http_workspace):
    context, page = asked(browser, real_http_workspace, FRAMEWORK, EXTRAS)
    yield page
    context.close()


def sent(page, text):
    page.locator(".message-body p").filter(has_text=re.compile("^" + re.escape(text) + "$")).wait_for()
    return page.locator(".message-body p").filter(has_text=re.compile("^" + re.escape(text) + "$")).count()


def test_a_forwarded_question_offers_its_options_and_other(question):
    assert question.locator(".ask-option strong").all_inner_texts() == ["React", "Vue", "Other"]


def test_a_single_answer_question_offers_radio_buttons(question):
    assert question.locator(".ask-option input[type=radio]").count() == 3


def test_a_multi_answer_question_offers_checkboxes(two_questions):
    assert two_questions.locator(".ask-question").nth(1).locator("input[type=checkbox]").count() == 3


def test_an_option_shows_its_description(question):
    assert question.locator(".ask-description").first.inner_text() == "Most used"


def test_a_forwarded_question_hides_its_marker(question):
    assert "one-conv-ask" not in question.locator(".message-body").first.inner_text()


def test_nothing_can_be_sent_before_choosing(question):
    assert question.locator(".ask-send").is_disabled()


def test_a_chosen_option_is_sent_to_the_agent(question):
    question.locator(".ask-option", has_text="Vue").click()
    question.locator(".ask-send").click()
    assert sent(question, "Vue") == 1


def test_a_typed_answer_is_sent_to_the_agent(question):
    question.locator(".ask-other").fill("Svelte")
    question.locator(".ask-send").click()
    assert sent(question, "Svelte") == 1


def test_a_sent_form_cannot_be_sent_again(question):
    question.locator(".ask-option", has_text="React").click()
    question.locator(".ask-send").click()
    sent(question, "React")
    assert question.locator(".ask-send").is_disabled()


def test_several_questions_are_answered_by_header(two_questions):
    two_questions.locator(".ask-option", has_text="Vue").click()
    two_questions.locator(".ask-option", has_text="Router").click()
    two_questions.locator(".ask-option", has_text="Store").click()
    two_questions.locator(".ask-send").click()
    two_questions.locator(".message-body p", has_text="Extras: Router, Store").wait_for()
    assert "Framework: Vue" in two_questions.locator(".message-body p", has_text="Extras: Router, Store").inner_text()
