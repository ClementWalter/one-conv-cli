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
"""Pairing, browser authority and session boundaries use only disposable credentials."""

import concurrent.futures
import logging
import sqlite3
import stat

import pytest
from starlette.testclient import TestClient

from one_conv.control.auth import COOKIE, AuthStore
from one_conv.control.server import BODY_LIMIT, application, canonical_origin

ORIGIN = "https://workspace.example.test"


@pytest.fixture
def auth(tmp_path):
    return AuthStore(tmp_path)


@pytest.fixture
def app(tmp_path):
    return application(tmp_path, ORIGIN, providers={"workers": lambda method, payload: {"method": method, "payload": payload}})


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def paired(client, app):
    token = app.state.auth.issue_pairing_token()
    client.post("/api/login", json={"token": token}, headers={"Origin": ORIGIN}).raise_for_status()
    csrf = client.get("/api/session").json()["csrf_token"]
    return client, {"Origin": ORIGIN, "X-One-Conv-Token": csrf}


def test_pairing_is_one_use(auth):
    token = auth.issue_pairing_token()
    auth.pair(token)
    assert auth.pair(token) is None


def test_pairing_hash_only_is_persisted(auth):
    token = auth.issue_pairing_token()
    assert token.encode() not in auth.path.read_bytes()


def test_browser_session_hash_only_is_persisted(auth):
    cookie = auth.pair(auth.issue_pairing_token())
    assert cookie.encode() not in auth.path.read_bytes()


def test_pairing_claim_is_atomic_under_concurrent_requests(auth):
    token = auth.issue_pairing_token()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        cookies = list(pool.map(auth.pair, [token] * 8))
    assert sum(cookie is not None for cookie in cookies) == 1


def test_new_pairing_supersedes_unused_links(auth):
    old = auth.issue_pairing_token()
    auth.issue_pairing_token()
    assert auth.pair(old) is None


def test_pairing_expiration_is_enforced(tmp_path):
    now = [100.0]
    auth = AuthStore(tmp_path, clock=lambda: now[0])
    token = auth.issue_pairing_token(10)
    now[0] = 110.0
    assert auth.pair(token) is None


def test_session_expiration_is_enforced(tmp_path):
    now = [100.0]
    auth = AuthStore(tmp_path, session_seconds=10, clock=lambda: now[0])
    cookie = auth.pair(auth.issue_pairing_token())
    now[0] = 110.0
    assert auth.session(cookie) is None


def test_session_survives_application_restart(auth):
    cookie = auth.pair(auth.issue_pairing_token())
    assert AuthStore(auth.path.parent).session(cookie) == auth.session(cookie)


def test_revocation_survives_restart(auth):
    cookie = auth.pair(auth.issue_pairing_token())
    auth.revoke_all_sessions()
    assert AuthStore(auth.path.parent).session(cookie) is None


def test_repairing_revokes_the_previous_browser_session(auth):
    previous = auth.pair(auth.issue_pairing_token())
    auth.pair(auth.issue_pairing_token(), previous)
    assert auth.session(previous) is None


@pytest.mark.parametrize("value", [0, -1, 3601, True, "600"])
def test_invalid_operator_pairing_ttl_is_rejected(auth, value):
    with pytest.raises(ValueError):
        auth.issue_pairing_token(value)


def test_auth_database_is_private(auth):
    assert stat.S_IMODE(auth.path.stat().st_mode) == 0o600


def test_anonymous_session_contains_no_workspace_data(client):
    assert client.get("/api/session").json() == {"authenticated": False, "human": None}


@pytest.mark.parametrize("path", ["/api/state", "/api/direct", "/api/topics/private", "/api/providers/workers"])
def test_data_apis_require_browser_authentication(client, path):
    assert client.get(path).status_code == 401


def test_shell_is_public(client):
    assert client.get("/").status_code == 200


def test_health_discloses_only_service_health(client):
    assert client.get("/health").json() == {"ok": True}


@pytest.mark.parametrize("origin", [None, "https://evil.example.test", "null", ORIGIN + "/", "https://workspace.example.test:444"])
def test_pairing_requires_exact_browser_origin(client, app, origin):
    headers = {} if origin is None else {"Origin": origin}
    assert client.post("/api/login", json={"token": app.state.auth.issue_pairing_token()}, headers=headers).status_code == 403


@pytest.mark.parametrize("attribute", ["__host-oneconv-session=", "httponly", "secure", "samesite=strict", "path=/"])
def test_login_cookie_has_browser_security_attributes(client, app, attribute):
    response = client.post("/api/login", json={"token": app.state.auth.issue_pairing_token()}, headers={"Origin": ORIGIN})
    cookie = response.headers["set-cookie"].lower()
    assert attribute in cookie


def test_session_exposes_the_published_human_return_route(paired):
    client, _ = paired
    assert client.get("/api/session").json()["human"] == {
        "address": "user", "route": "workspace/user", "name": "You", "kind": "human"}


def test_csrf_token_does_not_disclose_the_browser_cookie(paired):
    client, _ = paired
    assert client.get("/api/session").json()["csrf_token"] != client.cookies.get(COOKIE)


def test_pairing_token_has_no_browser_cookie_authority(client, app):
    token = app.state.auth.issue_pairing_token()
    assert client.get("/api/state", headers={"Cookie": f"{COOKIE}={token}"}).status_code == 401


def test_browser_cookie_is_not_a_pairing_token(paired):
    client, _ = paired
    assert client.post("/api/login", json={"token": client.cookies.get(COOKIE)}, headers={"Origin": ORIGIN}).status_code == 401


def test_pairing_tokens_are_not_logged(client, app, caplog):
    token = app.state.auth.issue_pairing_token()
    caplog.set_level(logging.DEBUG)
    client.post("/api/login", json={"token": token}, headers={"Origin": ORIGIN}).raise_for_status()
    assert token not in caplog.text


def test_provider_capabilities_reflect_configured_callbacks(paired):
    client, _ = paired
    assert client.get("/api/session").json()["capabilities"] == {"workers": True, "accounting": False, "pairing": True, "channels": True, "changes": True, "unread": True, "acks": True, "sign_out_others": True, "stop": True, "routines": True}


def test_anonymous_browser_cannot_create_a_device_invite(client):
    assert client.post("/api/devices/pair", json={}, headers={"Origin": ORIGIN}).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Origin": ORIGIN},
                                    {"Origin": "https://evil.example.test", "X-One-Conv-Token": "wrong"}])
def test_device_invites_require_origin_and_csrf(paired, headers):
    client, _ = paired
    assert client.post("/api/devices/pair", json={}, headers=headers).status_code == 403


def test_node_bearer_cannot_create_a_browser_device_invite(client, app):
    token = app.state.hub.add_token("worker")
    assert client.post("/api/devices/pair", json={}, headers={"Origin": ORIGIN,
                                                           "Authorization": "Bearer " + token}).status_code == 401


@pytest.mark.parametrize("payload", [{"origin": "https://evil.example.test"}, {"access": "guest"}, {"ttl": 3600}])
def test_device_invite_authority_cannot_be_changed_by_the_browser(paired, payload):
    client, headers = paired
    assert client.post("/api/devices/pair", json=payload, headers=headers).status_code == 400


@pytest.fixture
def device_invite(paired):
    client, headers = paired
    response = client.post("/api/devices/pair", json={}, headers=headers)
    response.raise_for_status()
    return client, headers, response


def test_device_invite_uses_only_the_configured_origin(device_invite):
    assert device_invite[2].json()["url"].startswith(ORIGIN + "/#pair=")


def test_device_invite_explicitly_grants_owner_access(device_invite):
    assert device_invite[2].json()["access"] == "owner"


def test_device_invite_is_never_cacheable(device_invite):
    assert device_invite[2].headers["cache-control"] == "no-store"


def test_issuing_a_device_invite_keeps_the_current_browser_connected(device_invite):
    assert device_invite[0].get("/api/session").json()["authenticated"] is True


def test_device_invite_connects_a_second_browser(device_invite, app):
    token = device_invite[2].json()["url"].split("#pair=", 1)[1]
    with TestClient(app, base_url=ORIGIN) as second:
        second.post("/api/login", json={"token": token}, headers={"Origin": ORIGIN}).raise_for_status()
        assert second.get("/api/session").json()["authenticated"] is True


def test_device_invite_cannot_connect_two_new_browsers(device_invite, app):
    token = device_invite[2].json()["url"].split("#pair=", 1)[1]
    with TestClient(app, base_url=ORIGIN) as second:
        second.post("/api/login", json={"token": token}, headers={"Origin": ORIGIN}).raise_for_status()
    assert device_invite[0].post("/api/login", json={"token": token}, headers={"Origin": ORIGIN}).status_code == 401


def test_device_invite_expires_after_ten_minutes(paired, app):
    client, headers = paired
    app.state.auth.clock = lambda: 100.0
    response = client.post("/api/devices/pair", json={}, headers=headers)
    token = response.json()["url"].split("#pair=", 1)[1]
    app.state.auth.clock = lambda: 700.0
    assert app.state.auth.pair(token) is None


def test_new_device_invite_supersedes_only_the_previous_unused_invite(device_invite):
    client, headers, previous = device_invite
    client.post("/api/devices/pair", json={}, headers=headers).raise_for_status()
    token = previous.json()["url"].split("#pair=", 1)[1]
    assert client.post("/api/login", json={"token": token}, headers={"Origin": ORIGIN}).status_code == 401


def test_device_invite_token_is_not_logged(paired, caplog):
    client, headers = paired
    caplog.set_level(logging.DEBUG)
    response = client.post("/api/devices/pair", json={}, headers=headers)
    token = response.json()["url"].split("#pair=", 1)[1]
    assert token not in caplog.text


def test_browser_cookie_cannot_authenticate_the_node_api(paired):
    client, _ = paired
    assert client.get("/v1/sessions").status_code == 401


def test_node_bearer_token_cannot_authenticate_browser_apis(client, app):
    token = app.state.hub.add_token("worker")
    assert client.get("/api/state", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_wrong_host_is_rejected_even_for_public_shell(client):
    assert client.get("/", headers={"Host": "evil.example.test"}).status_code == 421


def test_forwarded_host_cannot_change_authority(client):
    assert client.get("/health", headers={"Host": "evil.example.test", "X-Forwarded-Host": "workspace.example.test"}).status_code == 421


def test_duplicate_host_headers_are_rejected(client):
    assert client.get("/health", headers=[("Host", "workspace.example.test"), ("Host", "evil.example.test")]).status_code == 421


@pytest.mark.parametrize("headers", [{}, {"Origin": ORIGIN}, {"Origin": "https://evil.example.test", "X-One-Conv-Token": "wrong"}])
def test_provider_writes_require_origin_and_csrf(paired, headers):
    client, _ = paired
    assert client.post("/api/providers/workers", json={}, headers=headers).status_code == 403


def test_provider_get_receives_query_parameters(paired):
    client, _ = paired
    assert client.get("/api/providers/workers?view=summary").json() == {"method": "GET", "payload": {"view": "summary"}}


def test_provider_post_receives_only_authenticated_payload(paired):
    client, headers = paired
    assert client.post("/api/providers/workers", json={"action": "list"}, headers=headers).json() == {
        "method": "POST", "payload": {"action": "list"}}


def test_logout_revokes_a_session(paired):
    client, headers = paired
    client.post("/api/logout", json={}, headers=headers).raise_for_status()
    assert client.get("/api/state").status_code == 401


def test_revoked_session_cannot_read_after_server_restart(paired, app, tmp_path):
    client, _ = paired
    cookie = client.cookies.get(COOKIE)
    app.state.auth.revoke_all_sessions()
    restarted = TestClient(application(tmp_path, ORIGIN), base_url=ORIGIN)
    assert restarted.get("/api/state", headers={"Cookie": f"{COOKIE}={cookie}"}).status_code == 401


def test_expired_session_cannot_read_private_state(paired, app):
    client, _ = paired
    with sqlite3.connect(app.state.auth.path) as db:
        db.execute("UPDATE browser_sessions SET expires=0")
    assert client.get("/api/state").status_code == 401


@pytest.mark.parametrize("body", [b"[]", b"null", b'{"token":NaN}', b'{"token":"a","token":"b"}', b"\xff"])
def test_malformed_login_bodies_are_rejected(client, body):
    assert client.post("/api/login", content=body, headers={"Origin": ORIGIN, "Content-Type": "application/json"}).status_code == 400


def test_streamed_login_body_is_bounded(client):
    chunks = iter([b'{"token":"', b"a" * BODY_LIMIT, b'"}'])
    assert client.post("/api/login", content=chunks, headers={"Origin": ORIGIN, "Content-Type": "application/json"}).status_code == 413


def test_compressed_json_is_not_accepted(client):
    assert client.post("/api/login", content=b"{}", headers={"Origin": ORIGIN, "Content-Type": "application/json",
                                                           "Content-Encoding": "gzip"}).status_code == 415


def test_data_responses_are_never_cacheable(paired):
    client, _ = paired
    assert client.get("/api/state").headers["cache-control"] == "no-store"


def test_static_assets_cannot_override_private_api_routes(tmp_path):
    with pytest.raises(ValueError):
        application(tmp_path, ORIGIN, assets={"/api/state": (b"private", "text/plain")})


def test_service_worker_has_root_scope_and_no_cache(tmp_path):
    app = application(tmp_path, ORIGIN, assets={"/sw.js": (b"self.addEventListener('fetch',()=>{});", "text/javascript")})
    response = TestClient(app, base_url=ORIGIN).get("/sw.js")
    assert (response.headers["service-worker-allowed"], response.headers["cache-control"]) == ("/", "no-store")


def test_default_tls_port_is_canonicalized():
    assert canonical_origin("https://workspace.example.test:443") == ORIGIN


@pytest.mark.parametrize("origin", ["http://public.example.test", "https://user:pass@workspace.test", "https://workspace.test/path",
                                   "https://workspace.test?token=value", "https://WORKSPACE.test", "https://workspace.test:99999",
                                   "https://@workspace.test", "https://workspace.test:", "https://workspace.test."])
def test_invalid_public_origins_are_rejected(origin):
    with pytest.raises(ValueError):
        canonical_origin(origin)
