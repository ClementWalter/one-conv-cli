"""Exercise human conversations and workspace navigation in a real isolated browser."""

import json
import os
import socket
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from one_conv.live import router, team, web
from one_conv.live.model import LiveSession


@pytest.fixture(scope="module")
def browser():
    """Use a disposable browser profile; the caller may supply an installed executable."""
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(headless=True, executable_path=os.getenv("ONE_CONV_TEST_BROWSER"))
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Install a Playwright browser or set ONE_CONV_TEST_BROWSER")
            raise
        yield instance
        instance.close()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A loopback workspace whose two agents only receive files in disposable inboxes."""
    for source, variable in (("live", "ONE_CONV_LIVE_DIR"), ("omp", "OMP_HOME"),
                             ("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"),
                             ("cursor", "ONE_CONV_CURSOR_HOME")):
        directory = tmp_path / source
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    for variable in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN", "ONE_CONV_HUB_NODE"):
        monkeypatch.delenv(variable, raising=False)
    registry = tmp_path / "live" / "omp"
    registry.mkdir()
    for ident, title in (("browser-builder", "Build the release"), ("browser-reviewer", "Review the release")):
        (registry / f"{ident}.json").write_text(json.dumps({"sessionId": ident, "pid": os.getpid(),
                                                        "cwd": "workspace", "title": title, "status": "idle"}))
    team.set_name("builder", "omp:browser-builder", lambda _: True)
    team.set_name("reviewer", "omp:browser-reviewer", lambda _: True)
    team.join("design-review", "omp:browser-builder")
    team.record("design-review", "a" * 32, "omp:browser-builder", "The release is ready for your review.")
    router.log_message({"id": "b" * 32, "at": time.time(), "from": "omp:browser-builder",
                        "to": "omp:browser-reviewer", "text": "Please check the implementation."})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web.application(port), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("The browser fixture server did not start")
        time.sleep(0.01)
    yield {"url": f"http://127.0.0.1:{port}", "home": tmp_path}
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


@pytest.fixture
def page(browser, workspace):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    active = context.new_page()
    active.goto(workspace["url"], wait_until="networkidle")
    active.locator(".message").first.wait_for()
    yield active
    context.close()


@pytest.fixture
def posted(page):
    """Submit a real form through the browser and the production message endpoint."""
    page.get_by_role("button", name="New message", exact=True).click()
    page.locator(".recipient-option").filter(has_text="builder").click()
    page.locator("#message-input").fill("A message from the human.")
    with page.expect_response("**/api/messages") as response:
        page.get_by_role("button", name="Send message as a human").click()
    return response.value.json()


def test_workspace_opens_a_real_channel(page):
    assert page.locator("#title").inner_text() == "design-review"


def test_human_composer_sends_without_an_agent_identity(posted):
    assert posted["receipt"]["from"] == "user"


def test_browser_send_reaches_the_agent_inbox(posted, workspace):
    directory = workspace["home"] / "live" / "inbox" / "omp" / "browser-builder"
    assert len(list(directory.glob("*.json"))) == 1


def test_browser_send_labels_the_envelope_as_human(posted, workspace):
    directory = workspace["home"] / "live" / "inbox" / "omp" / "browser-builder"
    message = json.loads(next(directory.glob("*.json")).read_text())["text"]
    assert 'from="user" kind="human"' in message


def test_human_message_is_visible_in_the_conversation(page, posted):
    page.locator(".message-author").filter(has_text="You").wait_for()
    assert page.locator(".message-body").filter(has_text="A message from the human.").count() == 1


def test_agent_reply_reaches_the_human_conversation(page, posted):
    router.send("user", "I received your message.", LiveSession("omp", "browser-builder"))
    page.locator(".message-body").filter(has_text="I received your message.").wait_for()
    assert page.locator(".message-body").filter(has_text="I received your message.").count() == 1


def test_channel_composer_reaches_its_member(page, workspace):
    page.locator("#message-input").fill("A human message for the channel.")
    with page.expect_response("**/api/messages") as response:
        page.get_by_role("button", name="Send message as a human").click()
    assert response.value.json()["receipt"]["topic"] == "design-review"


def test_search_finds_real_message_content(page):
    page.locator("#search").fill("ready for your review")
    page.locator("#search").press("Enter")
    page.locator("#notice").filter(has_text="1 message found").wait_for()
    assert page.locator(".message-body").filter(has_text="The release is ready").count() == 1


def test_theme_choice_survives_reload(page):
    page.get_by_role("button", name="Switch to dark theme").click()
    page.reload(wait_until="networkidle")
    assert page.locator("html").get_attribute("data-theme") == "dark"


def test_channel_draft_is_preserved_when_opening_a_direct_message(page):
    page.locator("#message-input").fill("A draft for the channel, not a DM.")
    page.get_by_role("button", name="New message", exact=True).click()
    page.locator(".recipient-option").filter(has_text="builder").click()
    page.locator("#topics .conversation-row").first.click()
    assert page.locator("#message-input").input_value() == "A draft for the channel, not a DM."


def test_lost_response_retry_keeps_its_id_after_sending_another_conversation(page, workspace):
    """A response lost after acceptance must not turn an explicit retry into another wake."""
    attempts = []

    def lose_first_response(route):
        message = route.request.post_data_json
        attempts.append(message)
        if len(attempts) == 1:
            route.fetch()
            route.abort()
        else:
            route.continue_()

    page.route("**/api/messages", lose_first_response)
    page.get_by_role("button", name="New message", exact=True).click()
    page.locator(".recipient-option").filter(has_text="builder").click()
    page.locator("#message-input").fill("Keep the original send identity.")
    page.get_by_role("button", name="Send message as a human").click()
    page.locator("#composer-feedback.error").wait_for()
    page.get_by_role("button", name="New message", exact=True).click()
    page.locator(".recipient-option").filter(has_text="reviewer").click()
    page.locator("#message-input").fill("An unrelated message.")
    with page.expect_response("**/api/messages"):
        page.get_by_role("button", name="Send message as a human").click()
    page.locator("#message-input").wait_for(state="visible")
    page.get_by_role("button", name="New message", exact=True).click()
    page.locator(".recipient-option").filter(has_text="builder").click()
    # Once the stored copy is visible, the restored draft is cleared, so nothing can wake the agent twice.
    page.locator(".message-body").filter(has_text="Keep the original send identity.").wait_for()
    directory = workspace["home"] / "live" / "inbox" / "omp" / "browser-builder"
    assert len(list(directory.glob("*.json"))) == 1


def test_a_lost_response_retry_from_the_bubble_keeps_its_id(page, workspace):
    """Retry on the bubble sends the same id, so the agent is woken once."""
    def lose_first_response(route):
        route.fetch()
        route.abort()

    page.route("**/api/messages", lose_first_response, times=1)
    page.get_by_role("button", name="New message", exact=True).click()
    page.locator(".recipient-option").filter(has_text="builder").click()
    page.locator("#message-input").fill("Retry from the bubble.")
    page.get_by_role("button", name="Send message as a human").click()
    retry = page.locator("#messages li").filter(has_text="Retry from the bubble.").get_by_role("button", name="Retry")
    retry.wait_for()
    with page.expect_response("**/api/messages"):
        retry.click()
    directory = workspace["home"] / "live" / "inbox" / "omp" / "browser-builder"
    assert len(list(directory.glob("*.json"))) == 1


def test_activity_updates_while_it_stays_open(page):
    page.get_by_role("button", name="Activity", exact=True).click()
    page.locator(".message-body").filter(has_text="Please check the implementation.").wait_for()
    router.send("user", "Fresh activity without changing views.", LiveSession("omp", "browser-builder"))
    page.locator(".message-body").filter(has_text="Fresh activity without changing views.").wait_for()
    assert page.locator(".message-body").filter(has_text="Fresh activity without changing views.").count() == 1


def test_phone_layout_has_no_horizontal_overflow(page):
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_phone_navigation_opens_on_request(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Open workspace sidebar").click()
    assert "sidebar-open" in page.locator("body").get_attribute("class")


def test_message_markup_cannot_execute_in_the_browser(page):
    router.send("#design-review", '<img src=x onerror="window.humanLeak=true">', LiveSession("omp", "browser-builder"))
    page.locator(".message-body").filter(has_text="<img src=x").wait_for()
    assert page.locator(".message-body img").count() == 0
