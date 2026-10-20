"""MCP tools preserve CLI boundaries and work through a real stdio connection."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

from one_conv import mcp_server


@pytest.fixture
def reader_stub(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '[{"uuid":"example"}]', 'Saved history only')

    monkeypatch.setattr(mcp_server.subprocess, "run", run)
    return calls


def test_reader_returns_structured_history(reader_stub):
    assert mcp_server.read_cli("cloud", "chats")["data"] == [{"uuid": "example"}]


def test_reader_preserves_freshness(reader_stub):
    assert mcp_server.read_cli("cloud", "chats")["freshness"] == "saved"


def test_reader_protects_query_from_option_parsing(reader_stub):
    mcp_server.read_cli("cloud", "read", query="--refresh")
    assert reader_stub[0][0][-2:] == ["--", "--refresh"]


def test_read_keeps_unread_state(reader_stub):
    mcp_server.read_cli("cloud", "read", query="example")
    assert "--no-mark-read" in reader_stub[0][0]


def test_local_read_uses_thread(reader_stub):
    mcp_server.read_cli("local", "read", query="project", session="abc")
    assert reader_stub[0][0][4] == "thread"


@pytest.mark.parametrize("fields", [
    {"namespace": "invalid"}, {"source": "claude"}, {"limit": 0}, {"limit": 1001},
    {"namespace": "local", "refresh": True}, {"session": "abc"},
])
def test_invalid_request_is_rejected(fields):
    with pytest.raises(ToolError):
        mcp_server.read_cli(**({"namespace": "cloud", "command": "chats"} | fields))


def test_reader_reports_timeout(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("reader", 60)

    monkeypatch.setattr(mcp_server.subprocess, "run", run)
    with pytest.raises(ToolError, match="timed out"):
        mcp_server.read_cli("cloud", "chats")


def test_reader_reports_cli_error(monkeypatch):
    monkeypatch.setattr(mcp_server.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, "", "No conversation matches"))
    with pytest.raises(ToolError, match="No conversation matches"):
        mcp_server.read_cli("cloud", "read", query="missing")


@pytest.fixture
def saved_history(tmp_path):
    account = tmp_path / "account"
    account.mkdir()
    (account / "example.json").write_text(json.dumps({
        "source": "claude-chat", "session": "mcp-example", "cwd": "claude-chat:test",
        "title": "MCP example", "turns": [
            {"role": "user", "text": "Hello MCP", "ts": "2026-01-01T00:00:00Z"},
        ],
    }))
    return tmp_path


def protocol_call(cache, tool, arguments):
    """Use process pipes to exercise initialization, discovery, and real CLI delegation."""
    async def call():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-O", str(mcp_server.LAUNCHER), "mcp"],
            env={**os.environ, "ONE_CONV_CLOUD_CACHE": str(cache)},
        )
        async with Client(parameters) as client:
            if tool is None:
                return await client.list_tools()
            return await client.call_tool(tool, arguments)

    return asyncio.run(call())


def test_stdio_discovers_tools(saved_history):
    result = protocol_call(saved_history, None, {})
    assert {tool.name for tool in result.tools} == {
        "list_chats", "read_conversation", "search_conversations", "find_conversations", "list_accounts",
        "list_live_sessions", "send_message", "set_name", "join_topic", "list_topics",
        "ack",
    }


@pytest.mark.parametrize("tool,arguments", [
    ("list_chats", {}), ("read_conversation", {"query": "mcp-example"}),
    ("search_conversations", {"query": "Hello MCP"}),
    ("find_conversations", {"query": "MCP example"}),
])
def test_stdio_reads_saved_history(saved_history, tool, arguments):
    result = protocol_call(saved_history, tool, {"source": "claude-chat", **arguments})
    assert "mcp-example" in json.dumps(result.structured_content)


def test_stdio_reports_errors(saved_history):
    result = protocol_call(saved_history, "read_conversation", {"query": "missing", "source": "claude-chat"})
    assert result.is_error


def test_generated_config_starts_server():
    result = subprocess.run([sys.executable, "-O", str(mcp_server.LAUNCHER), "mcp", "--config"],
                            capture_output=True, text=True, check=True, timeout=30)
    config = json.loads(result.stdout)["mcpServers"]["one-conv"]

    async def discover():
        async with Client(StdioServerParameters(**config)) as client:
            return await client.list_tools()

    assert len(asyncio.run(discover()).tools) == 11


def test_stdio_send_message_names_codex_caller_from_tool_meta(tmp_path):
    """Codex identifies its thread only in each tool call's `_meta`."""
    live = tmp_path / "live"
    (live / "omp").mkdir(parents=True)
    (live / "omp" / "o1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "o1", "cwd": "/work"}))

    async def send():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-O", str(mcp_server.LAUNCHER), "mcp"],
            env={**os.environ, "ONE_CONV_LIVE_DIR": str(live), "CODEX_HOME": str(tmp_path / "codex")})
        async with Client(parameters) as client:
            return await client.call_tool("send_message", {"to": "omp:o1", "text": "hi"},
                                          meta={"threadId": "t-123"})

    asyncio.run(send())
    [message] = (live / "inbox" / "omp" / "o1").glob("*.json")
    assert 'from="codex:t-123"' in json.loads(message.read_text())["text"]


def test_stdio_send_message_fyi_leaves_notice(tmp_path):
    live = tmp_path / "live"
    (live / "omp").mkdir(parents=True)
    (live / "omp" / "o1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "o1", "cwd": "/work"}))

    async def send():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-O", str(mcp_server.LAUNCHER), "mcp"],
            env={**os.environ, "ONE_CONV_LIVE_DIR": str(live), "CODEX_HOME": str(tmp_path / "codex")})
        async with Client(parameters) as client:
            return await client.call_tool("send_message", {"to": "omp:o1", "text": "merged", "fyi": True},
                                          meta={"threadId": "t-123"})

    asyncio.run(send())
    assert json.loads((live / "fyi" / "omp-o1.jsonl").read_text())["from"] == "codex:t-123"


def call_in_process(tool, arguments):
    """Call one tool on an in-process server, as a client sees it: (is_error, text)."""
    async def call():
        async with Client(mcp_server.create_server()) as client:
            result = await client.call_tool(tool, arguments)
            return result.is_error, " ".join(getattr(item, "text", "") for item in result.content)
    return asyncio.run(call())


def test_a_refused_tool_call_tells_the_agent_why():
    _, text = call_in_process("ack", {"message_id": "a" * 32})
    assert text.startswith("Error executing tool ack: ")
