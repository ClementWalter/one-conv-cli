"""Images from the phone: uploaded over real HTTPS, pasted into the composer, shown in the thread; the viewport fit."""

import base64

import pytest

from one_conv.control.auth import AuthStore
from tests import test_control_messages as https_tests
from tests import test_pwa_browser as phone_tests

pytest.importorskip("playwright.sync_api")

tls_certificate = https_tests.tls_certificate
real_http_workspace = https_tests.real_http_workspace
browser = phone_tests.browser

# A 1x1 PNG, enough for an upload.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def upload(workspace, body=PNG, media_type="image/png"):
    return workspace["browser"].post("/api/attachments", content=body,
                                     headers={**workspace["headers"], "Content-Type": media_type})


# --- HTTP ----------------------------------------------------------------------------------------------


def test_an_image_uploads_over_https(real_http_workspace):
    assert upload(real_http_workspace).json()["media_type"] == "image/png"


def test_an_uploaded_image_is_served_back_with_its_type(real_http_workspace):
    ident = upload(real_http_workspace).json()["id"]
    response = real_http_workspace["browser"].get(f"/api/attachments/{ident}")
    assert (response.headers["content-type"], response.content) == ("image/png", PNG)


def test_a_non_image_upload_is_refused(real_http_workspace):
    assert upload(real_http_workspace, b"%PDF", "application/pdf").status_code == 415


def test_an_upload_needs_the_session_token(real_http_workspace):
    headers = {"Origin": real_http_workspace["headers"]["Origin"], "Content-Type": "image/png"}
    assert real_http_workspace["browser"].post("/api/attachments", content=PNG, headers=headers).status_code == 403


def test_fetching_an_image_needs_a_session(real_http_workspace):
    import httpx
    ident = upload(real_http_workspace).json()["id"]
    with httpx.Client(base_url=real_http_workspace["origin"], verify=False) as anonymous:
        assert anonymous.get(f"/api/attachments/{ident}").status_code == 401


def test_a_message_with_an_image_is_stored(real_http_workspace):
    ident = upload(real_http_workspace).json()["id"]
    response = real_http_workspace["browser"].post("/api/messages", json={"id": "c" * 32, "target": "#general", "text": "",
                                                                          "attachments": [ident]},
                                                   headers=real_http_workspace["headers"])
    assert response.json()["status"] == "sent"


# --- browser -------------------------------------------------------------------------------------------


@pytest.fixture
def phone(browser, real_http_workspace):
    context = browser.new_context(viewport={"width": 390, "height": 740}, is_mobile=True, has_touch=True,
                                  ignore_https_errors=True, service_workers="block")
    page = context.new_page()
    token = AuthStore(real_http_workspace["directory"]).issue_pairing_token()
    page.goto(real_http_workspace["origin"] + "/#pair=" + token, wait_until="networkidle")
    page.get_by_role("button", name="Connect this device", exact=True).click()
    page.locator("#pairing").wait_for(state="hidden")
    page.goto("about:blank")
    page.goto(real_http_workspace["origin"] + "/#topic=general")
    page.locator("#composer:not([hidden]) #message-input").wait_for()
    yield page
    context.close()


PASTE = """async (png) => {
  const bytes = Uint8Array.from(atob(png), (c) => c.charCodeAt(0));
  const file = new File([bytes], "shot.png", { type: "image/png" });
  const transfer = new DataTransfer();
  transfer.items.add(file);
  const event = new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true });
  document.getElementById("message-input").dispatchEvent(event);
}"""


@pytest.fixture
def pasted(phone):
    phone.evaluate(PASTE, base64.b64encode(PNG).decode())
    phone.locator(".attachment-chip:not(.uploading):not(.failed)").wait_for()
    return phone


def test_a_pasted_image_shows_as_an_attachment(pasted):
    assert pasted.locator(".attachment-chip .attachment-thumb").count() == 1


def test_a_pasted_image_alone_enables_sending(pasted):
    assert pasted.locator("#send-message").is_enabled()


def test_a_sent_image_appears_in_the_thread(pasted):
    pasted.get_by_role("button", name="Send message as a human").click()
    pasted.locator("#messages .message-image").wait_for()
    assert "/api/attachments/" in pasted.locator("#messages .message-image").get_attribute("src")


def test_a_sent_image_leaves_the_composer(pasted):
    pasted.get_by_role("button", name="Send message as a human").click()
    pasted.locator("#messages .message-image").wait_for()
    assert pasted.locator("#attachments").is_hidden()


def test_a_removed_image_is_not_sent(pasted):
    pasted.locator(".attachment-remove").click()
    assert pasted.locator("#send-message").is_disabled()


def test_the_attach_button_opens_the_file_picker(phone):
    with phone.expect_file_chooser() as chooser:
        phone.get_by_role("button", name="Attach an image").click()
    assert chooser.value.is_multiple


def test_a_picked_image_shows_as_an_attachment(phone, tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    phone.locator("#attach-input").set_input_files(str(tmp_path / "shot.png"))
    phone.locator(".attachment-chip:not(.uploading):not(.failed)").wait_for()
    assert phone.locator(".attachment-chip").count() == 1


def test_the_layout_follows_the_visual_viewport(phone):
    phone.evaluate("() => window.visualViewport.dispatchEvent(new Event('resize'))")
    assert phone.evaluate("() => document.documentElement.style.getPropertyValue('--viewport-height')") == \
        phone.evaluate("() => Math.round(window.visualViewport.height) + 'px'")


def test_a_closed_keyboard_takes_no_height(phone):
    phone.evaluate("() => window.visualViewport.dispatchEvent(new Event('resize'))")
    assert phone.evaluate("() => document.documentElement.style.getPropertyValue('--keyboard-height')") == "0px"


def test_the_installed_layout_rules_come_after_the_phone_rules():
    from one_conv.live import web
    assert web.STYLE.rindex("@media (display-mode: standalone)") > web.STYLE.rindex("@media (max-width: 760px)")


def test_the_installed_top_bar_keeps_its_controls_below_the_status_bar():
    from one_conv.live import web
    installed = web.STYLE[web.STYLE.rindex("@media (display-mode: standalone)"):]
    assert "padding-top: env(safe-area-inset-top)" in installed
