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
"""A share used by another agent: the share's MCP server on both relays, `live share` setup output, sends to a link."""

import asyncio
import json
import re
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("uvicorn")

from one_conv.live import router, share
from test_live_share import SESSION, call, call_tool, hub, inbox, live, machine, shared  # noqa: F401

LINK = "https://relay.example/s/0123456789abcdef#" + "s" * 43


def rpc(shared, method, params=None, secret=None, ident=1):
    """(status, body) of one JSON-RPC request to the share's MCP server."""
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}, **({"id": ident} if ident is not None else {})}
    status, _, body = call(shared.url, f"/s/{shared.share['id']}/mcp", method="POST", body=message,
                           headers={"Authorization": f"Bearer {secret or shared.share['secret']}",
                                    "Accept": "application/json, text/event-stream"})
    return status, body


def tool(shared, name, **arguments):
    """The structured result of one tool call, or its error text."""
    result = rpc(shared, "tools/call", {"name": name, "arguments": arguments})[1]["result"]
    return result["content"][0]["text"] if result.get("isError") else result["structuredContent"]


# --- the share's MCP server, on both backends ---------------------------------------------------------

def test_the_mcp_server_needs_a_bearer_secret(shared):
    status, _, _ = call(shared.url, f"/s/{shared.share['id']}/mcp", method="POST", body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 401


def test_the_mcp_server_refuses_a_wrong_secret(shared):
    assert rpc(shared, "ping", secret="not-the-secret")[0] == 404


def test_the_mcp_server_of_a_revoked_share_answers_410(shared):
    call(shared.url, f"/v1/shares/{shared.share['id']}", shared.mac, "DELETE")
    assert rpc(shared, "ping")[0] == 410


def test_initialize_warns_that_the_session_is_another_persons_agent(shared):
    assert "another person's agent" in rpc(shared, "initialize", {"protocolVersion": "2025-06-18"})[1]["result"]["instructions"]


def test_the_mcp_server_lists_four_tools(shared):
    tools = rpc(shared, "tools/list")[1]["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["ask", "read_replies", "send", "session_info"]


def test_a_notification_is_accepted_without_a_body(shared):
    assert rpc(shared, "notifications/initialized", ident=None)[0] == 202


def test_send_reaches_the_bound_session(shared):
    tool(shared, "send", text="hello from an agent")
    assert inbox(shared)[0]["kind"] == "guest"


def test_send_returns_a_cursor(shared):
    assert tool(shared, "send", text="hello")["after"] >= 1


def test_ask_without_an_answer_is_pending(shared):
    assert tool(shared, "ask", text="anyone?", wait_seconds=0)["status"] == "pending"


def test_read_replies_returns_answers_after_the_cursor(shared):
    after = tool(shared, "send", text="question")["after"]
    call(shared.url, "/v1/messages", shared.mac, "POST",
         {"id": uuid.uuid4().hex, "to": f"guest:{shared.share['id']}", "from": "claude:aaa", "text": "answer"})
    assert [r["text"] for r in tool(shared, "read_replies", after=after)["replies"]] == ["answer"]


def test_session_info_names_the_session_by_its_name(shared):
    assert tool(shared, "session_info")["name"] == "lead"


def test_session_info_hides_the_title_without_the_transcript(shared):
    assert "title" not in tool(shared, "session_info")


def test_session_info_says_it_is_online(shared):
    assert tool(shared, "session_info")["online"] is True


def test_a_tool_refused_by_the_share_is_a_tool_error(shared):
    assert "8 KB" in tool(shared, "send", text="x" * 8193)


def test_an_unknown_tool_is_a_protocol_error(shared):
    assert rpc(shared, "tools/call", {"name": "read_transcript"})[1]["error"]["code"] == -32602


@pytest.fixture
def answered_while_waiting(shared):
    """A guest GET waiting with wait=10 while the session answers 0.5 s later: (seconds waited, body)."""
    after = tool(shared, "send", text="question")["after"]

    def answer_later():
        time.sleep(0.5)
        call(shared.url, "/v1/messages", shared.mac, "POST",
             {"id": uuid.uuid4().hex, "to": f"guest:{shared.share['id']}", "from": "claude:aaa", "text": "later"})
    threading.Thread(target=answer_later).start()
    started = time.monotonic()
    _, _, body = call(shared.url, f"/v1/shares/{shared.share['id']}/messages?after={after}&wait=10",
                      headers={"X-Share-Secret": shared.share["secret"]})
    return time.monotonic() - started, body


def test_a_waiting_guest_get_returns_the_answer(answered_while_waiting):
    assert answered_while_waiting[1]["messages"][-1]["text"] == "later"


def test_a_waiting_guest_get_returns_once_the_answer_lands(answered_while_waiting):
    assert answered_while_waiting[0] < 5


# --- the client side ----------------------------------------------------------------------------------

def test_a_share_link_is_recognised():
    assert share.is_link(LINK)


def test_a_redacted_link_has_no_secret():
    assert share.redacted(LINK) == "https://relay.example/s/0123456789abcdef"


@pytest.mark.parametrize("link", ["https://relay.example/s/0123456789abcdef", "https://relay.example/s/xyz#" + "s" * 43])
def test_a_malformed_link_is_not_a_share_link(link):
    assert not share.is_link(link)


def test_a_plain_http_link_to_a_remote_host_is_refused():
    with pytest.raises(share.ShareError):
        share.parse_link("http://relay.example/s/0123456789abcdef#" + "s" * 43)


def test_the_claude_setup_passes_the_secret_as_a_bearer_header():
    assert '--header "Authorization: Bearer ' + "s" * 43 + '"' in share.setup(LINK, "Alice")["claude_command"]


def test_the_claude_setup_points_at_the_share_mcp_url():
    assert " https://relay.example/s/0123456789abcdef/mcp " in share.setup(LINK, "Alice")["claude_command"]


def test_the_codex_setup_is_a_config_block_with_the_bearer_header():
    assert 'http_headers = { Authorization = "Bearer ' in share.setup(LINK, "Alice")["codex_config"]


@pytest.mark.parametrize(("label", "name"), [("Alice", "one-conv-share-alice"), ("Bob's team!", "one-conv-share-bob-s-team"),
                                             ("!!!", "one-conv-share-01234567")])
def test_the_server_name_comes_from_the_label(label, name):
    assert share.server_name(label, "0123456789abcdef") == name


def test_a_send_to_a_link_without_yes_is_a_dry_run():
    assert "a session shared with you" in live("send", LINK, "hi").output


def test_a_dry_run_to_a_link_never_prints_the_secret():
    assert "s" * 43 not in live("send", LINK, "hi").output


@pytest.mark.parametrize("extra", [{"fyi": True}, {"when": "idle"}])
def test_a_send_to_a_link_is_plain_text_only(extra):
    with pytest.raises(router.LiveError):
        router.send(LINK, "hi", None, **extra)


# --- end to end: another agent talks to a shared stub session -------------------------------------

@pytest.fixture
def answering(machine, monkeypatch):
    """The stub session answers each guest with send_message to the address its envelope names, while a bridge runs."""
    def deliver(session, text):
        machine.received.append(text)
        address = re.search(r'to="(guest:[0-9a-f]{16})"', text)[1]
        call_tool("send_message", {"to": address, "text": f"answer #{len(machine.received)}"})
        return "queued"
    monkeypatch.setitem(router.ADAPTERS, "omp", SimpleNamespace(deliver=deliver))
    stop = threading.Event()

    def run():
        while not stop.is_set():
            machine.bridge.step(wait=0.2)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    made = json.loads(live("share", "--label", "Alice", "--yes", "--json").output)
    yield SimpleNamespace(**vars(machine), made=made)
    stop.set()
    thread.join(timeout=10)


@pytest.fixture
def asked(answering):
    """A colleague's MCP client (the SDK's streamable-HTTP client) asks the share's MCP server one question."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def run():
        headers = {"Authorization": f"Bearer {answering.made['secret']}"}
        async with create_mcp_http_client(headers=headers) as http:
            async with Client(streamable_http_client(answering.made["mcp_url"], http_client=http)) as client:
                return await client.call_tool("ask", {"text": "what are you working on?", "wait_seconds": 30})
    return SimpleNamespace(result=asyncio.run(run()), received=answering.received)


def test_ask_returns_the_sessions_answer(asked):
    assert asked.result.structured_content["reply"] == "answer #1"


def test_ask_reaches_the_session_as_a_guest(asked):
    assert 'kind="guest"' in asked.received[0]


def test_a_send_to_the_link_prints_the_answer_inline(answering):
    assert "answer #1" in live("send", answering.made["link"], "status?", "--yes").output


def test_a_send_to_the_link_without_waiting_only_posts(answering):
    receipt = json.loads(live("send", answering.made["link"], "fyi", "--yes", "--no-wait", "--json").output)
    assert (receipt["status"], "reply" in receipt) == ("sent", False)


def test_the_receipt_of_a_send_to_the_link_has_no_secret(answering):
    output = live("send", answering.made["link"], "status?", "--yes", "--json").output
    assert answering.made["secret"] not in output


def test_live_share_prints_the_claude_setup(machine):
    assert "claude mcp add --transport http one-conv-share-alice" in live("share", "--label", "Alice", "--yes").output


def test_live_share_marks_the_secret_lines(machine):
    assert live("share", "--label", "Alice", "--yes").output.count(share.SECRET_MARK) >= 4
