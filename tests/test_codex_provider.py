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
"""Codex cloud contract tests use source-evidenced current-turn payloads."""

import pytest

from one_conv.providers.base import SchemaChanged
from one_conv.providers.codex import CAPABILITIES, CodexProvider


class SessionStub:
    """Record HTTP requests without credentials or provider network access."""

    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return ResponseStub(self.payload)


class ResponseStub:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.fixture
def task():
    return {
        "task": {"id": "task_123", "title": "Refactor cloud task client"},
        "current_user_turn": {"input_items": [{
            "type": "message", "role": "user",
            "content": [{"content_type": "text", "text": "First line"},
                        {"content_type": "text", "text": "Second line"}],
        }]},
        "current_assistant_turn": {"output_items": [{
            "type": "message",
            "content": [{"content_type": "text", "text": "Assistant response"}],
        }]},
    }


def test_lists_only_source_evidenced_current_tasks():
    session = SessionStub({"items": [], "cursor": None})
    CodexProvider(session, "account").list_conversations(cursor="next", limit=20)
    assert session.requests[0] == (
        "https://chatgpt.com/backend-api/wham/tasks/list",
        {"params": {"limit": 20, "task_filter": "current", "cursor": "next"},
         "timeout": 20, "allow_redirects": False},
    )


def test_next_cursor_is_preserved():
    provider = CodexProvider(SessionStub({"items": [], "cursor": "next"}), "account")
    assert provider.list_conversations().next_cursor == "next"


@pytest.mark.parametrize(("requested", "sent"), [(1, 1), (20, 20), (50, 20), (100, 20)])
def test_page_limit_respects_upstream_maximum(requested, sent):
    session = SessionStub({"items": [], "cursor": None})
    CodexProvider(session, "account").list_conversations(limit=requested)
    assert session.requests[0][1]["params"]["limit"] == sent


def test_validation_discloses_partial_coverage():
    provider = CodexProvider(SessionStub({"items": []}), "account")
    assert provider.validate_connection().capabilities == CAPABILITIES


@pytest.mark.parametrize("index,expected", [(0, "First line\n\nSecond line"), (1, "Assistant response")])
def test_reads_current_turn_text(task, index, expected):
    provider = CodexProvider(SessionStub(task), "account")
    assert provider.read_current_turn("task_123").messages[index].text == expected


def test_preserves_provider_message_id(task):
    task["current_user_turn"]["input_items"][0]["id"] = "message-id"
    provider = CodexProvider(SessionStub(task), "account")
    assert provider.read_current_turn("task_123").messages[0].id == "message-id"


def test_reads_worklog_fallback(task):
    task["current_assistant_turn"] = {"worklog": {"messages": [{
        "author": {"role": "assistant"}, "content": {"parts": ["Worklog answer"]},
    }]}}
    provider = CodexProvider(SessionStub(task), "account")
    assert provider.read_current_turn("task_123").messages[-1].text == "Worklog answer"


def test_encodes_task_identifier(task):
    task["task"]["id"] = "task/a?b"
    session = SessionStub(task)
    CodexProvider(session, "account").read_current_turn("task/a?b")
    assert session.requests[0][0].endswith("/tasks/task%2Fa%3Fb")


@pytest.mark.parametrize("payload", [{}, {"items": {}}, {"items": [{}]}, {"items": [], "cursor": 3}])
def test_rejects_unknown_list_shape(payload):
    with pytest.raises(SchemaChanged):
        CodexProvider(SessionStub(payload), "account").list_conversations()


def test_rejects_wrong_task_identity(task):
    with pytest.raises(SchemaChanged, match="identity mismatch"):
        CodexProvider(SessionStub(task), "account").read_current_turn("other")


def test_missing_turn_fields_is_not_empty_history():
    payload = {"task": {"id": "task_123", "title": "Task"}}
    with pytest.raises(SchemaChanged, match="turns missing"):
        CodexProvider(SessionStub(payload), "account").read_current_turn("task_123")


def test_current_turn_is_explicitly_incomplete(task):
    assert CodexProvider(SessionStub(task), "account").read_current_turn("task_123").complete is False


@pytest.mark.parametrize("limit", [0, 101, True, "10"])
def test_rejects_invalid_page_limit(limit):
    with pytest.raises(ValueError):
        CodexProvider(SessionStub({}), "account").list_conversations(limit=limit)


class GraphSessionStub(SessionStub):
    """Serve task metadata and the separately fetched turn graph."""

    def __init__(self, task, graph):
        super().__init__(task)
        self.graph = graph

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return ResponseStub(self.graph if url.endswith("/turns") else self.payload)


@pytest.fixture
def graph():
    return {"current_turn_id": "answer", "turn_mapping": {
        "question": {"id": "question", "parent": None, "children": ["answer", "abandoned"],
                     "turn": {"id": "question", "role": "user", "input_items": [{
                         "type": "message", "content": [{"content_type": "text", "text": "Question"}],
                     }]}},
        "answer": {"id": "answer", "parent": "question", "children": [],
                   "turn": {"id": "answer", "role": "assistant", "output_items": [{
                       "type": "message", "content": [{"content_type": "text", "text": "Accepted"}],
                   }]}},
        "abandoned": {"id": "abandoned", "parent": "question", "children": [],
                      "turn": {"id": "abandoned", "role": "assistant", "output_items": [{
                          "type": "message", "content": [{"content_type": "text", "text": "Discarded"}],
                      }]}},
    }}


def test_graph_fetches_observed_turns_route(task, graph):
    session = GraphSessionStub(task, graph)
    CodexProvider(session, "account").read_conversation("task_123")
    assert session.requests[-1][0] == "https://chatgpt.com/backend-api/wham/tasks/task_123/turns"


def test_graph_retains_alternate_branch(task, graph):
    result = CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")
    assert [message.id for message in result.messages] == ["question", "answer", "abandoned"]


def test_graph_identifies_active_branch(task, graph):
    result = CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")
    assert result.active_leaf_id == "answer"


def test_graph_preserves_parent(task, graph):
    result = CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")
    assert result.messages[1].parent_id == "question"


def test_graph_declares_actual_coverage(task, graph):
    result = CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")
    assert result.coverage == "task_turn_graph"


def test_graph_missing_active_turn_fails(task, graph):
    graph["current_turn_id"] = "unknown"
    with pytest.raises(SchemaChanged, match="current turn missing"):
        CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")


def test_graph_missing_current_field_is_not_empty_history(task):
    with pytest.raises(SchemaChanged, match="current turn field missing"):
        CodexProvider(GraphSessionStub(task, {"turn_mapping": {}}), "account").read_conversation("task_123")


def test_graph_missing_parent_fails(task, graph):
    graph["turn_mapping"]["answer"]["parent"] = "unknown"
    with pytest.raises(SchemaChanged, match="parent missing"):
        CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")


def test_graph_cycle_fails(task, graph):
    graph["turn_mapping"]["question"]["parent"] = "answer"
    with pytest.raises(SchemaChanged, match="cycle"):
        CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")


def test_graph_identity_mismatch_fails(task, graph):
    graph["turn_mapping"]["question"]["turn"]["id"] = "other"
    with pytest.raises(SchemaChanged, match="identity mismatch"):
        CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")


def test_graph_worklog_only_answer(task, graph):
    graph["turn_mapping"]["answer"]["turn"] = {"id": "answer", "role": "assistant", "worklog": {
        "messages": [{"author": {"role": "assistant"}, "content": {"parts": ["Worklog"]}}],
    }}
    result = CodexProvider(GraphSessionStub(task, graph), "account").read_conversation("task_123")
    assert result.messages[1].text == "Worklog"
