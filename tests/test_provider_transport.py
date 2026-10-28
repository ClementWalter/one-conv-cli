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
"""Provider transport failures never masquerade as an empty conversation archive."""

from types import SimpleNamespace

import pytest

from one_conv.providers.base import (AuthenticationRequired, JsonTransport, ProviderUnavailable,
                                     RateLimited, SchemaChanged, retry_response)
from one_conv.providers.chatgpt import ChatGPTProvider


class StubSession:
    def __init__(self, statuses, payload=None, headers=None):
        self.statuses = iter(statuses)
        self.payload = {} if payload is None else payload
        self.headers = headers or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=next(self.statuses), headers=self.headers,
                               json=lambda: self.payload)


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_errors_are_typed(status):
    with pytest.raises(AuthenticationRequired):
        JsonTransport(StubSession([status]), "https://example.com", "fixture").get("/read")


def test_rate_limit_honors_retry_after():
    delays = []
    JsonTransport(StubSession([429, 200], headers={"Retry-After": "2"}), "https://example.com",
                  "fixture", sleep=delays.append).get("/read")
    assert delays == [2.0]


def test_long_rate_limit_is_returned_without_sleep():
    with pytest.raises(RateLimited) as error:
        JsonTransport(StubSession([429], headers={"Retry-After": "120"}), "https://example.com",
                      "fixture").get("/read")
    assert error.value.retry_after == 120


def test_retry_count_is_bounded():
    session = StubSession([503, 503, 503])
    with pytest.raises(ProviderUnavailable):
        JsonTransport(session, "https://example.com", "fixture", sleep=lambda _: None).get("/read")
    assert len(session.calls) == 3


def test_redirect_is_not_followed():
    session = StubSession([302])
    with pytest.raises(ProviderUnavailable):
        JsonTransport(session, "https://example.com", "fixture").get("/read")
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("path", ["https://attacker.example", "//attacker.example", "/\\attacker"])
def test_external_paths_are_rejected(path):
    with pytest.raises(ValueError):
        JsonTransport(StubSession([]), "https://example.com", "fixture").get(path)


@pytest.mark.parametrize("payload", [[], {}, {"items": None}, {"items": [{}]}])
def test_chatgpt_schema_drift_is_visible(payload):
    with pytest.raises(SchemaChanged):
        ChatGPTProvider(StubSession([200], payload)).list_conversations()


def test_chatgpt_empty_live_page_advances_to_archive():
    assert ChatGPTProvider(StubSession([200], {"items": []})).list_conversations().next_cursor == "1:0"


def test_chatgpt_empty_archive_finishes():
    assert ChatGPTProvider(StubSession([200], {"items": []})).list_conversations("1:0").next_cursor is None


def test_chatgpt_preserves_message_branches():
    message = {"author": {"role": "assistant"}, "content": {"parts": ["answer"]}}
    payload = {"mapping": {"a": {"message": None}, "b": {"parent": "a", "message": message},
                           "c": {"parent": "a", "message": message}}, "current_node": "c"}
    result = ChatGPTProvider(StubSession([200], payload)).read_legacy_conversation("conversation")
    assert tuple((item.id, item.parent_id) for item in result.messages) == (("a", None), ("b", "a"), ("c", "a"))


def test_chatgpt_structural_leaf_keeps_parent_link():
    payload = {"mapping": {"root": {"message": {"author": {"role": "user"}, "content": {"parts": ["hello"]}}},
                           "leaf": {"parent": "root", "message": None}}, "current_node": "leaf"}
    result = ChatGPTProvider(StubSession([200], payload)).read_legacy_conversation("conversation")
    assert result.messages[1].parent_id == "root"


def test_chatgpt_search_preserves_snippet():
    payload = {"items": [{"conversation_id": "c", "payload": {"snippet": "found"}}]}
    assert ChatGPTProvider(StubSession([200], payload)).search_conversations("query")[0]["payload"]["snippet"] == "found"


def test_chatgpt_search_rejects_empty_object():
    with pytest.raises(SchemaChanged):
        ChatGPTProvider(StubSession([200], {})).search_conversations("query")


def test_chatgpt_search_rejects_repeated_cursor():
    payload = {"items": [{"conversation_id": "c"}], "cursor": "same"}
    with pytest.raises(SchemaChanged):
        ChatGPTProvider(StubSession([200, 200], payload)).search_conversations("query")


def test_compatibility_retry_returns_original_response():
    response = SimpleNamespace(status_code=200)
    assert retry_response(lambda: response) is response


def test_compatibility_retry_keeps_not_found_response():
    response = SimpleNamespace(status_code=404)
    assert retry_response(lambda: response) is response


def test_compatibility_retry_exhaustion_is_explicit():
    response = SimpleNamespace(status_code=429)
    assert retry_response(lambda: response, sleep=lambda _: None) is None


def test_compatibility_retry_honors_server_delay():
    responses = iter([SimpleNamespace(status_code=429, headers={"Retry-After": "10"}),
                      SimpleNamespace(status_code=200)])
    delays = []
    retry_response(lambda: next(responses), sleep=delays.append)
    assert delays == [10]


def test_chatgpt_account_identity():
    payload = {"accessToken": "fixture-token", "account": {"id": "account"}, "user": {"email": "user@example.test"}}
    assert ChatGPTProvider(StubSession([200], payload)).validate_connection().id == "account"


def test_chatgpt_missing_account_is_not_connected():
    with pytest.raises(SchemaChanged):
        ChatGPTProvider(StubSession([200], {"accessToken": "fixture-token", "user": {}})).validate_connection()


def test_chatgpt_cookie_session_authorizes_api_requests():
    session = StubSession([200], {"accessToken": "fixture-token", "account": {"id": "account"}})
    ChatGPTProvider(session).validate_connection()
    assert session.headers == {"Authorization": "Bearer fixture-token", "ChatGPT-Account-ID": "account"}


def test_chatgpt_expired_cookie_session_requires_login():
    with pytest.raises(AuthenticationRequired):
        ChatGPTProvider(StubSession([200], {})).validate_connection()


@pytest.mark.parametrize("delay", ["nan", "inf", "-inf"])
def test_nonfinite_retry_after_uses_bounded_fallback(delay):
    delays = []
    JsonTransport(StubSession([429, 200], headers={"Retry-After": delay}), "https://example.com",
                  "fixture", sleep=delays.append).get("/read")
    assert delays == [1]


@pytest.mark.parametrize("payload", [None, [], {"mapping": []}, {"mapping": {}, "current_node": "missing"}])
def test_chatgpt_invalid_graph_fails(payload):
    with pytest.raises(SchemaChanged):
        ChatGPTProvider(StubSession([200], payload)).read_legacy_conversation("conversation")


def test_chatgpt_rich_content_is_retained():
    content = {"content_type": "multimodal_text", "parts": [{"asset_pointer": "asset"}]}
    payload = {"mapping": {"node": {"message": {"author": {"role": "user"}, "content": content}}}}
    result = ChatGPTProvider(StubSession([200], payload)).read_legacy_conversation("conversation")
    assert result.messages[0].content_blocks == (content,)


def message_page(identity, previous=False, following=False):
    return {"conversation_id": "conversation", "title": "Fixture", "messages": [
        {"id": identity, "author": {"role": "user"}, "content": {"parts": [identity]}}],
        "page_info": {"start_cursor": identity, "end_cursor": identity,
                      "has_previous_page": previous, "has_next_page": following}}


class PageTransport:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return next(self.pages)


def test_current_branch_pages_are_chronological():
    transport = PageTransport([message_page("new", True), message_page("old", following=True)])
    result = ChatGPTProvider(None, transport).read_conversation("conversation")
    assert tuple(row.id for row in result.messages) == ("old", "new")


def test_current_branch_uses_observed_before_parameter():
    transport = PageTransport([message_page("new", True), message_page("old", following=True)])
    ChatGPTProvider(None, transport).read_conversation("conversation")
    assert transport.calls[1] == ("/backend-api/conversations/conversation/messages",
                                  {"before": "new", "include_has_versions": "true", "num_turns": 10})


def test_current_branch_page_bound_reports_incomplete():
    result = ChatGPTProvider(None, PageTransport([message_page("new", True)])).read_conversation("conversation", max_pages=1)
    assert result.complete is False


def test_current_branch_coverage_is_explicit():
    result = ChatGPTProvider(None, PageTransport([message_page("new")])).read_conversation("conversation")
    assert result.coverage == "current_branch"


def test_current_branch_does_not_invent_parent_edges():
    result = ChatGPTProvider(None, PageTransport([message_page("new")])).read_conversation("conversation")
    assert result.messages[0].parent_id is None


def test_current_branch_duplicate_pages_fail():
    with pytest.raises(SchemaChanged):
        ChatGPTProvider(None, PageTransport([message_page("new", True), message_page("new")])).read_conversation("conversation")


class SearchTransport:
    def __init__(self, response):
        self.response = response
        self.body = None

    def post_search(self, path, body):
        self.body = body
        return self.response


@pytest.fixture
def global_search_payload():
    return {"items": [{"source_type": "conversation", "snippet": "match",
                       "payload": {"conversation_id": "conversation"}},
                      {"source_type": "project"}], "cursor": "more", "partial_results": False,
            "source_statuses": []}


def test_global_search_filters_nonconversation_sources(global_search_payload):
    result = ChatGPTProvider(None, SearchTransport(global_search_payload)).global_search("query")
    assert [item["conversation_id"] for item in result["items"]] == ["conversation"]


def test_global_search_reports_unfetched_pages(global_search_payload):
    result = ChatGPTProvider(None, SearchTransport(global_search_payload)).global_search("query")
    assert result["complete"] is False


def test_global_search_request_has_observed_source_filters(global_search_payload):
    transport = SearchTransport(global_search_payload)
    ChatGPTProvider(None, transport).global_search("query")
    assert transport.body["source_requests"] == [{"type": "conversation"}, {"type": "project"},
        {"type": "library", "filters": {"lanes": ["image", "document", "folder"], "providers": ["native"]}}]


@pytest.mark.parametrize("origin,path", [("https://claude.ai", "/backend-api/global/search"),
                                         ("https://chatgpt.com", "/backend-api/conversation")])
def test_transport_refuses_other_posts(origin, path):
    with pytest.raises(ValueError):
        JsonTransport(None, origin, "fixture").post_search(path, {})


def test_readonly_search_post_retries_same_query_body():
    class Session:
        def __init__(self):
            self.requests = []

        def post(self, url, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(status_code=429 if len(self.requests) == 1 else 200,
                                   headers={}, json=lambda: {})

    session = Session()
    JsonTransport(session, "https://chatgpt.com", "fixture", sleep=lambda _: None).post_search(
        "/backend-api/global/search", {"query_id": "fixed", "query": "query"})
    assert [call["json"] for call in session.requests] == [{"query_id": "fixed", "query": "query"}] * 2


def test_patient_transport_waits_out_long_rate_limit():
    delays = []
    JsonTransport(StubSession([429, 200], headers={"Retry-After": "120"}), "https://example.com",
                  "fixture", sleep=delays.append, max_retry_delay=600).get("/read")
    assert delays == [120.0]
