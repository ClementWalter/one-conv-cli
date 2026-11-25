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
#     "numpy==2.5.4",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""The Worker's remote MCP server as the Claude app reaches it, under `wrangler dev --local`.

A client registers dynamically, the owner types the passphrase on /authorize, the code is
exchanged with PKCE, and the MCP Python SDK's streamable HTTP client then calls the tools with
that bearer token. Runs only with ONE_CONV_TEST_RELAY_WORKER=1; nothing reaches Cloudflare.
"""

import base64
import hashlib
import json
import re
import secrets
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import anyio
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from relay_worker import MCP_PASSPHRASE, _request

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
CAPTURE = json.loads((Path(__file__).resolve().parents[1] / "relay-worker" / "test" / "fixtures" / "capture.json").read_text())


class _Stop(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # The redirect goes to the client; the test reads its Location instead.


def _http(url: str, data: dict | None = None, headers: dict | None = None, form: bool = True):
    """(status, headers, body) of one request, redirects not followed."""
    body = None
    if data is not None:
        body = urlencode(data).encode() if form else json.dumps(data).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", **(headers or {})})
    try:
        with build_opener(_Stop).open(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        return error.code, error.headers, error.read()


def _authorize(url: str) -> str:
    """An access token for `url`'s MCP endpoint, through registration, consent and the code exchange."""
    _, _, registered = _http(url + "/oauth/register", {"redirect_uris": [REDIRECT], "client_name": "Claude", "token_endpoint_auth_method": "none"}, form=False)
    client = json.loads(registered)["client_id"]
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urlencode({"response_type": "code", "client_id": client, "redirect_uri": REDIRECT, "code_challenge": challenge,
                       "code_challenge_method": "S256", "state": "s", "resource": url + "/mcp"})
    _, headers, page = _http(url + "/authorize?" + query)
    handle = re.search(r'name="handle" value="([^"]+)"', page.decode()).group(1)
    cookie = "; ".join(value.split(";")[0] for value in headers.get_all("Set-Cookie"))
    _, approved, _ = _http(url + "/authorize", {"handle": handle, "passphrase": MCP_PASSPHRASE, "decision": "approve"}, {"Cookie": cookie})
    code = parse_qs(urlsplit(approved["Location"]).query)["code"][0]
    _, _, token = _http(url + "/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client,
                                               "code_verifier": verifier, "resource": url + "/mcp"})
    return json.loads(token)["access_token"]


@pytest.fixture(scope="module")
def worker(relay_worker):
    """The Worker emptied, holding the fixture capture of node `mac` and a published @worker session on node `box`."""
    state = relay_worker.hub()
    admin = relay_worker.admin
    _request(relay_worker.url + "/archive/v1/chunks", "POST", admin, json.dumps({"chunks": CAPTURE["chunks"]}).encode())
    _request(relay_worker.url + "/archive/v1/conversations", "POST", admin, CAPTURE["commit"].encode())
    state.publish("box", [{"address": "claude:bbb", "name": "worker"}])
    return state


@pytest.fixture(scope="module")
def token(relay_worker, worker):
    return _authorize(relay_worker.url)


def _tools(url: str, token: str, calls: list[tuple[str, dict]]) -> list:
    """The results of `calls`, made in order over one MCP session with the SDK's streamable HTTP client."""
    async def run():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(url + "/mcp", http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if not calls:
                        return [tool.name for tool in (await session.list_tools()).tools]
                    results = []
                    for name, arguments in calls:
                        result = await session.call_tool(name, arguments)
                        results.append(result.content[0].text if result.is_error else json.loads(result.content[0].text))
                    return results
    return anyio.run(run)


def test_an_unauthenticated_request_is_challenged(relay_worker):
    assert _http(relay_worker.url + "/mcp", {}, form=False)[0] == 401


def test_the_sdk_client_lists_the_tools(relay_worker, token):
    assert _tools(relay_worker.url, token, []) == ["search_conversations", "read_conversation", "list_live_sessions", "send_message", "message_status"]


def test_search_finds_the_archived_conversation(relay_worker, token):
    assert [hit["key"] for hit in _tools(relay_worker.url, token, [("search_conversations", {"query": "forty-two"})])[0]["results"]] == [CAPTURE["conversation"]]


def test_read_returns_the_turns(relay_worker, token):
    result = _tools(relay_worker.url, token, [("read_conversation", {"key": CAPTURE["conversation"]})])[0]
    assert [turn["text"] for turn in result["turns"]] == ["Quartz café — naïve 🚀", 'Forty-two.\n\t"quoted"']


def test_live_sessions_list_the_published_name(relay_worker, token):
    assert [s["name"] for s in _tools(relay_worker.url, token, [("list_live_sessions", {"online_only": False})])[0]["sessions"]] == ["@worker"]


def test_a_sent_message_reaches_the_node_as_a_human(relay_worker, worker, token):
    _tools(relay_worker.url, token, [("send_message", {"to": "@worker", "text": "e2e ping"})])
    assert [m["from"] for m in worker.inbox("box", 0) if m["text"] == "e2e ping"] == ["claude-app/user"]
