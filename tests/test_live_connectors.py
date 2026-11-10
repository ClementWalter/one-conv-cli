#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "httpx==0.28.1",
#     "mcp==2.2.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""The node relay publishes chosen CLIs and answers the calls the hub queues, end to end over HTTP."""

import json
import socket
import subprocess
import threading

import httpx
import pytest
import uvicorn

from one_conv.control.runtime import application
from one_conv.live import connectors, hubclient

CATALOG = {"name": "demo-cli", "instructions": "Demo.",
           "tools": [{"name": "search", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}


class StubClient:
    """Record hub requests and answer the calls endpoint with queued calls."""

    def __init__(self, calls=()):
        self.requests, self.calls = [], list(calls)

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        if path.startswith("/v1/connector-calls?"):
            calls, self.calls = self.calls, []
            return {"calls": calls}
        return {}


def test_publish_sends_the_chosen_catalogs():
    client = StubClient()
    connectors.ConnectorRelay(["demo-cli"], catalog_of=lambda cli: CATALOG).publish(client)
    assert client.requests[0] == ("PUT", "/v1/connectors", {"catalogs": {"demo-cli": CATALOG}})


def test_publish_keeps_the_last_good_catalog():
    answers = iter([CATALOG, RuntimeError("introspection failed")])

    def catalog_of(cli):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    relay, client = connectors.ConnectorRelay(["demo-cli"], catalog_of=catalog_of), StubClient()
    relay.publish(client)
    relay.publish(client)
    assert client.requests[1][2] == {"catalogs": {"demo-cli": CATALOG}}


def test_calls_are_answered_with_their_result():
    call = {"id": "c1", "cli": "demo-cli", "tool": "search", "arguments": {"q": "x"}}
    client = StubClient([call])
    relay = connectors.ConnectorRelay(["demo-cli"], run=lambda c: {"content": [{"type": "text", "text": "ok"}]})
    relay.serve_calls(client)
    relay.pool.shutdown(wait=True)
    assert client.requests[-1] == ("POST", "/v1/connector-calls/c1", {"result": {"content": [{"type": "text", "text": "ok"}]}})


def test_a_failed_call_becomes_an_error_result(monkeypatch):
    monkeypatch.setattr(connectors, "mcp_bridge", lambda: "/nonexistent/mcp-bridge")
    assert connectors.run_call({"cli": "x", "tool": "y", "arguments": {}})["isError"] is True


def test_a_timed_out_call_says_so(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("mcp-bridge", 1)
    monkeypatch.setattr(connectors, "mcp_bridge", lambda: "mcp-bridge")
    monkeypatch.setattr(connectors.subprocess, "run", slow)
    assert "did not finish" in connectors.run_call({"cli": "x", "tool": "y", "arguments": {}})["content"][0]["text"]


def test_no_chosen_cli_starts_no_thread(monkeypatch):
    monkeypatch.setattr(connectors, "chosen", lambda: [])
    relay = connectors.ConnectorRelay()
    relay.connect("https://hub.example", "t", "macbook")
    assert relay.thread is None


def test_choose_keeps_each_cli_once():
    connectors.choose(["b-cli", "a-cli", "b-cli"])
    assert connectors.chosen() == ["a-cli", "b-cli"]


@pytest.fixture
def controller(tmp_path):
    """The real controller on a loopback port, with a node token and an access token for demo-cli."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path, origin)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.02)
    node = app.state.hub.add_token("macbook")
    access = app.state.connectors.grant("access", "claude", {"scopes": ["connectors"], "resource": origin + "/mcp/demo-cli"}, 600)
    yield origin, node, access
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


def test_tool_call_runs_on_the_node_end_to_end(controller):
    origin, node, access = controller
    relay = connectors.ConnectorRelay(["demo-cli"], catalog_of=lambda cli: CATALOG,
                                      run=lambda call: {"content": [{"type": "text", "text": f"found {call['arguments']['q']}"}]})
    relay.publish(hubclient.HubClient(origin, node, "macbook"))
    relay.connect(origin, node, "macbook")
    headers = {"Authorization": "Bearer " + access, "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    answer = httpx.post(origin + "/mcp/demo-cli", headers=headers, timeout=30,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "search", "arguments": {"q": "cats"}}})
    relay.stop()
    assert answer.json()["result"]["content"][0]["text"] == "found cats"
