#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "starlette==1.8.0",
#     "httpx==0.28.1",
#     "mcp==2.2.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Remote connectors: claude.ai signs in with OAuth, then tool calls are relayed to the node that serves the CLI."""

import base64
import hashlib
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from one_conv.control.connectors import CALLBACKS
from one_conv.control.runtime import application

ORIGIN = "https://workspace.example.test"
CALLBACK = CALLBACKS[0]
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
CATALOG = {"name": "demo-cli", "instructions": "Demo.",
           "tools": [{"name": "usage_guide", "inputSchema": {"type": "object", "properties": {}}},
                     {"name": "search", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}
MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
       "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture
def app(tmp_path):
    return application(tmp_path, ORIGIN)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as active:
        yield active


@pytest.fixture
def node_token(app):
    return app.state.hub.add_token("macbook")


@pytest.fixture
def published(client, node_token):
    client.put("/v1/connectors", json={"catalogs": {"demo-cli": CATALOG}},
               headers={"Authorization": "Bearer " + node_token}).raise_for_status()
    return node_token


def register(client, redirect=CALLBACK):
    return client.post("/register", json={"redirect_uris": [redirect], "client_name": "Claude",
                                          "token_endpoint_auth_method": "none",
                                          "grant_types": ["authorization_code", "refresh_token"],
                                          "response_types": ["code"]})


def start(client, resource=ORIGIN + "/mcp/demo-cli"):
    client_id = register(client).json()["client_id"]
    answer = client.get("/authorize", params={"response_type": "code", "client_id": client_id,
                                              "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
                                              "code_challenge_method": "S256", "state": "s1",
                                              "resource": resource})
    return client_id, parse_qs(urlsplit(answer.headers["location"]).query)["request"][0]


def pair(client, app):
    client.post("/api/login", json={"token": app.state.auth.issue_pairing_token()},
                headers={"Origin": ORIGIN}).raise_for_status()


def approve(client, request):
    return client.post("/oauth/consent", data={"request": request, "decision": "allow"}, headers={"Origin": ORIGIN})


def token_for(client, app, resource=ORIGIN + "/mcp/demo-cli"):
    pair(client, app)
    client_id, request = start(client, resource)
    code = parse_qs(urlsplit(approve(client, request).headers["location"]).query)["code"][0]
    answer = client.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                         "redirect_uri": CALLBACK, "code_verifier": VERIFIER, "resource": resource})
    return {**answer.json(), "client_id": client_id}


def rpc(client, token, method, params=None, cli="demo-cli"):
    return client.post(f"/mcp/{cli}", headers={**MCP, "Authorization": "Bearer " + token},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_authorization_server_metadata_names_the_register_endpoint(client):
    assert client.get("/.well-known/oauth-authorization-server").json()["registration_endpoint"] == ORIGIN + "/register"


def test_resource_metadata_names_the_cli_endpoint(client):
    assert client.get("/.well-known/oauth-protected-resource/mcp/demo-cli").json()["resource"] == ORIGIN + "/mcp/demo-cli"


def test_registration_refuses_another_redirect(client):
    assert register(client, "https://attacker.example/callback").status_code == 400


def test_registration_accepts_claude_callback(client):
    assert register(client).status_code == 201


def test_unauthenticated_endpoint_points_to_its_metadata(client, published):
    answer = client.post("/mcp/demo-cli", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert 'resource_metadata="https://workspace.example.test/.well-known/oauth-protected-resource/mcp/demo-cli"' \
        in answer.headers["www-authenticate"]


def test_consent_page_shows_without_a_session(client):
    _, request = start(client)
    assert "Allow" in client.get("/oauth/consent", params={"request": request}).text


def test_consent_needs_a_paired_browser(client):
    _, request = start(client)
    assert approve(client, request).status_code == 401


def test_consent_refuses_another_origin(client, app):
    pair(client, app)
    _, request = start(client)
    answer = client.post("/oauth/consent", data={"request": request, "decision": "allow"},
                         headers={"Origin": "https://attacker.example"})
    assert answer.status_code == 403


def test_consent_redirects_to_claude_with_the_state(client, app):
    pair(client, app)
    _, request = start(client)
    assert parse_qs(urlsplit(approve(client, request).headers["location"]).query)["state"] == ["s1"]


def test_consent_answer_may_submit_only_to_claude(client, app):
    pair(client, app)
    _, request = start(client)
    assert "form-action 'self' https://claude.ai" in approve(client, request).headers["content-security-policy"]


def test_denied_consent_returns_access_denied(client, app):
    pair(client, app)
    _, request = start(client)
    answer = client.post("/oauth/consent", data={"request": request, "decision": "deny"}, headers={"Origin": ORIGIN})
    assert parse_qs(urlsplit(answer.headers["location"]).query)["error"] == ["access_denied"]


def test_code_exchange_issues_a_bearer_token(client, app):
    assert token_for(client, app)["token_type"] == "Bearer"


def test_token_lists_the_cli_tools(client, app, published):
    token = token_for(client, app)["access_token"]
    names = [tool["name"] for tool in rpc(client, token, "tools/list").json()["result"]["tools"]]
    assert names == ["usage_guide", "search"]


def test_token_for_one_cli_is_refused_by_another(client, app, published, node_token):
    client.put("/v1/connectors", json={"catalogs": {"demo-cli": CATALOG, "other-cli": CATALOG}},
               headers={"Authorization": "Bearer " + node_token}).raise_for_status()
    token = token_for(client, app)["access_token"]
    assert rpc(client, token, "tools/list", cli="other-cli").status_code == 401


def refresh(client, tokens):
    return client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                       "client_id": tokens["client_id"]})


def test_refresh_token_issues_a_new_access_token(client, app):
    assert refresh(client, token_for(client, app)).status_code == 200


def test_refresh_token_works_once(client, app):
    tokens = token_for(client, app)
    refresh(client, tokens)
    assert refresh(client, tokens).status_code == 400


def test_refresh_token_is_bound_to_its_client(client, app):
    tokens = token_for(client, app)
    other = register(client).json()["client_id"]
    assert refresh(client, {**tokens, "client_id": other}).status_code == 400


def serve_one_call(client, node_token, reply):
    """Act as the node bridge: take the next call and answer it."""
    headers = {"Authorization": "Bearer " + node_token}
    calls = client.get("/v1/connector-calls", params={"wait": "5"}, headers=headers).json()["calls"]
    client.post(f"/v1/connector-calls/{calls[0]['id']}", headers=headers,
                json={"result": {"content": [{"type": "text", "text": reply(calls[0])}], "isError": False}})


def test_tool_call_is_relayed_to_the_node(client, app, published):
    token = token_for(client, app)["access_token"]
    node = threading.Thread(target=serve_one_call, args=(client, published, lambda call: f"hits for {call['arguments']['q']}"))
    node.start()
    answer = rpc(client, token, "tools/call", {"name": "search", "arguments": {"q": "cats"}})
    node.join()
    assert answer.json()["result"]["content"][0]["text"] == "hits for cats"


def test_call_to_an_offline_node_fails_at_once(client, app, published, monkeypatch):
    import one_conv.control.connectors as connectors
    monkeypatch.setattr(connectors, "LIVE_SECONDS", -1.0)
    token = token_for(client, app)["access_token"]
    assert rpc(client, token, "tools/call", {"name": "search", "arguments": {}}).json()["result"]["isError"] is True


def test_another_node_cannot_take_over_a_live_cli(client, app, published):
    other = app.state.hub.add_token("devbox")
    answer = client.put("/v1/connectors", json={"catalogs": {"demo-cli": CATALOG}},
                        headers={"Authorization": "Bearer " + other})
    assert answer.status_code == 409


def test_a_node_cannot_answer_another_nodes_call(client, app, published):
    other = app.state.hub.add_token("devbox")
    ident = app.state.connectors.enqueue("macbook", "demo-cli", "search", {})
    answer = client.post(f"/v1/connector-calls/{ident}", headers={"Authorization": "Bearer " + other},
                         json={"result": {"content": []}})
    assert answer.status_code == 404


def test_publish_needs_a_node_token(client):
    assert client.put("/v1/connectors", json={"catalogs": {}}).status_code == 401
