#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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
"""Authenticated HTTPS rate edits preserve current identity and worker ownership bindings."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from one_conv.control.runtime import application
from one_conv.hr import make_card
from tests import test_control_messages as https_tests

tls_certificate = https_tests.tls_certificate


@pytest.fixture
def workspace(tmp_path, tls_certificate):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"https://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, origin)
    certificate, key = tls_certificate
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1,
                                          ssl_certfile=str(certificate), ssl_keyfile=str(key)))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    browser = httpx.Client(base_url=origin, verify=False, timeout=5)
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("Accounting HTTPS fixture did not start")
            time.sleep(0.01)
        browser.post("/api/login", json={"token": app.state.auth.issue_pairing_token()},
                     headers={"Origin": origin}).raise_for_status()
        browser.headers.update({"Origin": origin, "X-One-Conv-Token": browser.get("/api/session").json()["csrf_token"]})
        yield browser, app.state.accounting
    finally:
        browser.close()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.fixture
def card():
    return make_card("jack", "Cached Jack", aliases=["cached-jack"], currency="USD", basis="allocated",
                     provenance="Fixture allocation", metering_basis="Occupied work hour")


@pytest.fixture
def stale_registration(workspace, card):
    browser, accounting = workspace
    created = browser.post("/api/providers/accounting", json={"action": "register", "card": card})
    created.raise_for_status()
    cached = created.json()["card"]
    current = accounting.rates.register({**card, "name": "Current Jack", "aliases": ["@current-jack"],
                                         "addresses": ["worker/codex:session"]}, replace=True)
    stale = {**cached, "hourly_rate": {**cached["hourly_rate"], "amount": "5"}}
    response = browser.post("/api/providers/accounting", json={"action": "register", "card": stale, "replace": True})
    return response, current, accounting


def test_https_stale_full_card_replacement_is_rejected(stale_registration):
    assert stale_registration[0].status_code == 400


def test_https_rejected_replacement_preserves_current_identity_and_bindings(stale_registration):
    _, current, accounting = stale_registration
    assert accounting.rates.show("jack") == current


def test_https_replacement_error_points_to_partial_rate_updates(stale_registration):
    assert "update_rate" in stale_registration[0].json()["error"]


@pytest.mark.parametrize("replacement", [{}, {"replace": False}])
def test_https_browser_can_create_a_rate_card(workspace, card, replacement):
    browser, _accounting = workspace
    assert browser.post("/api/providers/accounting", json={"action": "register", "card": card, **replacement}).status_code == 200


def test_https_browser_create_retry_is_idempotent(workspace, card):
    browser, accounting = workspace
    browser.post("/api/providers/accounting", json={"action": "register", "card": card}).raise_for_status()
    first = accounting.rates.list()
    browser.post("/api/providers/accounting", json={"action": "register", "card": card, "replace": False}).raise_for_status()
    assert accounting.rates.list() == first


def test_https_partial_rate_edit_preserves_current_worker_bindings(workspace, card):
    browser, accounting = workspace
    current = accounting.rates.register({**card, "name": "Current Jack", "addresses": ["worker/codex:session"]})
    response = browser.post("/api/providers/accounting", json={"action": "update_rate", "reference": "jack",
                                                             "hourly_rate": {**card["hourly_rate"], "amount": "5"}})
    response.raise_for_status()
    updated = accounting.rates.show("jack")
    assert (updated["name"], updated["addresses"], updated["hourly_rate"]["amount"]) == (current["name"], current["addresses"], "5")
