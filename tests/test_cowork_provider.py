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
"""Exercise Cowork snapshots through the provider boundary and shared cache."""

import json
from types import SimpleNamespace

import pytest

from one_conv.cloud import synchronize
from one_conv.providers.base import ProviderAccount, SchemaChanged
from one_conv.providers.cowork import CoworkProvider, _message, _page


@pytest.fixture
def event():
    return {"event_id": "event-1", "sequence_num": "1", "event_type": "user",
            "created_at": "2026-09-08T00:00:00Z",
            "payload": {"message": {"role": "user", "content": "Read this task"}}}


@pytest.fixture
def provider(event):
    provider = CoworkProvider(SimpleNamespace(headers={}), "org", authenticated_account=ProviderAccount("account", "Work", ("cowork:list",)))
    detail = {"id": "session", "title": "Test task", "tags": ["cowork-remote"]}
    pages = {"/v1/code/sessions": {"data": [detail]},
             "/v1/code/sessions/session": {"response_shape": detail},
             "/v1/code/sessions/session/events": {"data": [event], "resume_cursor": "1"}}
    provider.transport = SimpleNamespace(get=lambda path, params=None: pages[path])
    provider.identity = SimpleNamespace(validate_connection=lambda: ProviderAccount("account", "Work", ("cowork:list",)))
    return provider


def test_lists_only_cowork(provider):
    assert provider.list_conversations().items[0].id == "session"


def test_reads_user_text(provider):
    assert provider.read_conversation("session").messages[0].text == "Read this task"


def test_resume_cursor_alone_is_not_another_page(provider):
    assert provider.read_conversation("session").complete is True


def test_cache_integration(provider, tmp_path):
    synchronize(provider, "cowork-cloud", root=tmp_path)
    document = next(path for path in tmp_path.glob("*/*.json") if path.name != ".status.json")
    assert json.loads(document.read_text())["turns"][0]["text"] == "Read this task"


def test_follows_event_cursor(provider, event):
    calls = []
    original = provider.transport.get
    def get(path, params=None):
        if not path.endswith("/events"):
            return original(path, params)
        calls.append(params.get("cursor"))
        return {"data": [event], "next_cursor": "next"} if len(calls) == 1 else {"data": []}
    provider.transport.get = get
    provider.read_conversation("session")
    assert calls == [None, "next"]


def test_duplicate_events_fail(provider, event):
    original = provider.transport.get
    provider.transport.get = lambda path, params=None: {"data": [event, event]} if path.endswith("/events") else original(path, params)
    with pytest.raises(SchemaChanged, match="repeated"):
        provider.read_conversation("session")


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": [], "next_cursor": "next"}])
def test_invalid_pages_fail(payload):
    with pytest.raises(SchemaChanged):
        _page(payload)


def test_unknown_blocks_fail(event):
    event["payload"]["message"]["content"] = [{"type": "future_block"}]
    with pytest.raises(SchemaChanged):
        _message(event)


def test_tool_questions_are_readable(event):
    event["event_type"] = "assistant"
    event["payload"]["message"] = {"role": "assistant", "content": [{"type": "tool_use", "name": "AskUserQuestion", "input": {"question": "When?"}}]}
    assert "When?" in _message(event).text
