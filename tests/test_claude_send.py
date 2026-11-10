#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "click==8.3.3",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Sending into a Claude chat posts exactly one turn, after the active branch, and reports the reply."""

from copy import deepcopy
import json
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from one_conv import cloud_send
from one_conv.live.model import LiveSession
from one_conv.providers.anthropic import AnthropicProvider, ROOT_MESSAGE_ID
from one_conv.providers.base import (AuthenticationRequired, JsonTransport, ProviderUnavailable)

ORG = "940672dd-fe32-4630-abe5-50fb64796523"
CHAT = "27df5fd2-eddb-4422-9c73-75541dabb848"
COMPLETION = f"/api/organizations/{ORG}/chat_conversations/{CHAT}/completion"


def stream(*texts, stop=True):
    events = [{"type": "message_start", "message": {"uuid": "reply-1", "parent_uuid": "sent-1"}}]
    events += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}} for t in texts]
    events.append({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    if stop:
        events.append({"type": "message_stop"})
    return events


class StubTransport:
    """Serve a chat tree and a canned completion stream, recording what was posted."""

    def __init__(self, leaf="leaf-1", events=None):
        self.tree = {"uuid": CHAT, "current_leaf_message_uuid": leaf, "chat_messages": []}
        self.events = stream("po", "ng") if events is None else events
        self.posts = []

    def get(self, path, params=None):
        return deepcopy(self.tree)

    def post_completion(self, path, body):
        self.posts.append((path, body))
        yield from deepcopy(self.events)


@pytest.fixture
def transport():
    return StubTransport()


def test_send_posts_to_the_chat_completion(transport):
    AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hello")
    assert transport.posts[0][0] == COMPLETION


def test_send_continues_the_active_branch(transport):
    AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hello")
    assert transport.posts[0][1]["parent_message_uuid"] == "leaf-1"


def test_send_into_an_empty_chat_starts_at_the_root():
    transport = StubTransport(leaf=None)
    AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hello")
    assert transport.posts[0][1]["parent_message_uuid"] == ROOT_MESSAGE_ID


def test_send_posts_the_text_verbatim(transport):
    AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "<one-conv-message>x</one-conv-message>")
    assert transport.posts[0][1]["prompt"] == "<one-conv-message>x</one-conv-message>"


def test_send_returns_the_assembled_reply(transport):
    assert AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hello").reply == "pong"


def test_send_returns_the_sent_message_id(transport):
    assert AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hello").message_id == "sent-1"


def test_send_error_event_fails():
    transport = StubTransport(events=[{"type": "error", "error": {"type": "overloaded_error"}}])
    with pytest.raises(ProviderUnavailable, match="overloaded_error"):
        AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hello")


def test_send_truncated_stream_fails():
    transport = StubTransport(events=stream("po", stop=False))
    with pytest.raises(ProviderUnavailable, match="ended before"):
        AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hello")


class StubSession:
    """One HTTP response per POST, so a retry would show up as a second call."""

    def __init__(self, status, lines=()):
        self.status, self.lines, self.calls = status, lines, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=self.status, headers={}, iter_lines=lambda: iter(self.lines))


def test_completion_route_is_the_only_writable_path():
    with pytest.raises(ValueError):
        list(JsonTransport(StubSession(200), "https://claude.ai", "fixture").post_completion(
            f"/api/organizations/{ORG}/chat_conversations", {}))


def test_completion_is_refused_on_other_origins():
    with pytest.raises(ValueError):
        list(JsonTransport(StubSession(200), "https://chatgpt.com", "fixture").post_completion(COMPLETION, {}))


def test_completion_is_never_retried():
    session = StubSession(503)
    with pytest.raises(ProviderUnavailable):
        list(JsonTransport(session, "https://claude.ai", "fixture").post_completion(COMPLETION, {}))
    assert len(session.calls) == 1


def test_completion_authorization_error_is_typed():
    with pytest.raises(AuthenticationRequired):
        list(JsonTransport(StubSession(403), "https://claude.ai", "fixture").post_completion(COMPLETION, {}))


def test_completion_yields_data_events():
    session = StubSession(200, [b"event: ping", b'data: {"type": "ping"}', b""])
    assert list(JsonTransport(session, "https://claude.ai", "fixture").post_completion(COMPLETION, {})) == [{"type": "ping"}]


@pytest.fixture
def cache(tmp_path):
    def save(name, document):
        (tmp_path / "account").mkdir(exist_ok=True)
        (tmp_path / "account" / f"{name}.json").write_text(json.dumps(document))
    save("aaa111", {"source": "claude-chat", "id": CHAT, "session": "aaa111", "title": "Greek island stone house",
                    "account_id": "acct", "account_label": "Ariane"})
    save("bbb222", {"source": "claude-chat", "id": "chat-2", "session": "bbb222", "title": "Greek island ferries",
                    "account_id": "acct", "account_label": "Ariane"})
    save("ccc333", {"source": "chatgpt", "id": "chat-3", "session": "ccc333", "title": "Stone house", "account_id": "x"})
    return tmp_path


def test_saved_conversation_by_displayed_prefix(cache):
    assert cloud_send.saved_conversation("aaa", "claude-chat", cache)["id"] == CHAT


def test_saved_conversation_by_provider_prefix(cache):
    assert cloud_send.saved_conversation("27df5fd2", "claude-chat", cache)["session"] == "aaa111"


def test_saved_conversation_by_exact_title(cache):
    assert cloud_send.saved_conversation("greek island stone house", "claude-chat", cache)["id"] == CHAT


def test_saved_conversation_ignores_other_products(cache):
    with pytest.raises(click.ClickException):
        cloud_send.saved_conversation("ccc", "claude-chat", cache)


def test_saved_conversation_rejects_ambiguous_titles(cache):
    with pytest.raises(click.ClickException, match="Several"):
        cloud_send.saved_conversation("greek island", "claude-chat", cache)


def test_envelope_names_the_sender(monkeypatch):
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: LiveSession("claude", "abc", cwd="/Users/me/private"))
    assert '<one-conv-message from="claude:abc"' in cloud_send.wrap("hi", None)


def test_envelope_keeps_the_working_directory_local(monkeypatch):
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: LiveSession("claude", "abc", cwd="/Users/me/private"))
    assert "/Users/me/private" not in cloud_send.wrap("hi", None)


def test_envelope_asks_for_an_inline_answer(monkeypatch):
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: LiveSession("claude", "abc"))
    assert 'reply="inline"' in cloud_send.wrap("hi", None)


def test_dry_run_never_contacts_the_account(cache, monkeypatch):
    monkeypatch.setattr(cloud_send, "cache_root", lambda: cache)
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: None)
    contacted = []
    command = cloud_send.send_command("claude", "claude-chat")
    CliRunner().invoke(command, ["aaa", "hello"], obj={"claude_accounts": lambda: contacted.append(1) or iter(())})
    assert contacted == []


def test_dry_run_shows_the_target_chat(cache, monkeypatch):
    monkeypatch.setattr(cloud_send, "cache_root", lambda: cache)
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: None)
    result = CliRunner().invoke(cloud_send.send_command("claude", "claude-chat"), ["aaa", "hello"], obj={})
    assert "to:      Greek island stone house  (Ariane)" in result.output
