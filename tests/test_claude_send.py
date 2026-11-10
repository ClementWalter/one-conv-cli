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

import pytest
from click.testing import CliRunner

from one_conv import cloud_send
from one_conv.live import router
from one_conv.live.model import LiveSession
from one_conv.providers.anthropic import AnthropicProvider, ROOT_MESSAGE_ID
from one_conv.providers.base import (AuthenticationRequired, JsonTransport, ProviderUnavailable)

ORG = "11111111-2222-4333-8444-555555555555"
CHAT = "66666666-7777-4888-9999-aaaaaaaaaaaa"
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
    save("aaa111", {"source": "claude-chat", "id": CHAT, "session": "aaa111", "title": "Trip planning",
                    "account_id": "acct", "account_label": "Personal"})
    save("bbb222", {"source": "claude-chat", "id": "chat-2", "session": "bbb222", "title": "Trip planning budget",
                    "account_id": "acct", "account_label": "Personal"})
    save("ccc333", {"source": "chatgpt", "id": "chat-3", "session": "ccc333", "title": "Trip", "account_id": "x"})
    return tmp_path


def test_saved_conversation_by_displayed_prefix(cache):
    assert cloud_send.saved_conversation("aaa", "claude-chat", cache)["id"] == CHAT


def test_saved_conversation_by_provider_prefix(cache):
    assert cloud_send.saved_conversation("66666666", "claude-chat", cache)["session"] == "aaa111"


def test_saved_conversation_by_exact_title(cache):
    assert cloud_send.saved_conversation("trip planning", "claude-chat", cache)["id"] == CHAT


def test_saved_conversation_ignores_other_products(cache):
    with pytest.raises(cloud_send.CloudSendError):
        cloud_send.saved_conversation("ccc", "claude-chat", cache)


def test_saved_conversation_rejects_ambiguous_titles(cache):
    with pytest.raises(cloud_send.CloudSendError, match="Several"):
        cloud_send.saved_conversation("trip plan", "claude-chat", cache)


SENDER = LiveSession("claude", "abc", cwd="/Users/me/private")


def test_envelope_names_the_sender():
    assert '<one-conv-message from="claude:abc"' in cloud_send.cloud_envelope("hi", SENDER, "unknown")


def test_envelope_keeps_the_working_directory_local():
    assert "/Users/me/private" not in cloud_send.cloud_envelope("hi", SENDER, "unknown")


def test_envelope_asks_for_an_inline_answer():
    assert 'reply="inline"' in cloud_send.cloud_envelope("hi", SENDER, "unknown")


def test_envelope_keeps_no_reply():
    assert 'reply="none"' in cloud_send.cloud_envelope("hi", SENDER, "unknown", reply="none")


def test_envelope_without_agent_names_the_origin():
    assert '<one-conv-message from="macbook/claude-desktop"' in cloud_send.cloud_envelope("hi", None, "macbook/claude-desktop")


def test_deliver_without_registered_accounts_fails(cache, monkeypatch):
    monkeypatch.setattr(cloud_send, "cache_root", lambda: cache)
    monkeypatch.setattr(cloud_send, "_accounts", None)
    with pytest.raises(cloud_send.CloudSendError, match="unavailable"):
        cloud_send.deliver("aaa", "hi")


@pytest.fixture
def delivered(cache, monkeypatch, tmp_path):
    """Route live sends to a stub chat, recording the posted text."""
    monkeypatch.setattr(cloud_send, "cache_root", lambda: cache)
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    (tmp_path / "live").mkdir()
    posted = []

    def deliver(query, wrapped, accounts=None):
        posted.append((query, wrapped))
        return {"to": "claude-chat:" + CHAT, "title": "Trip planning", "account": "Personal", "account_id": "acct",
                "chat_message_id": "sent-1", "reply_id": "reply-1", "stop_reason": "end_turn", "reply": "pong",
                "url": "https://claude.ai/chat/" + CHAT}
    monkeypatch.setattr(cloud_send, "deliver", deliver)
    return posted


def test_live_send_routes_a_chat_address(delivered):
    router.send("claude-chat:aaa", "hello", SENDER)
    assert delivered[0][0] == "aaa"


def test_live_send_wraps_the_chat_message(delivered):
    router.send("claude-chat:aaa", "hello", SENDER, message_id="f" * 32)
    assert 'id="ffffffffffffffffffffffffffffffff"' in delivered[0][1]


def test_live_send_returns_the_chat_reply(delivered):
    assert router.send("claude-chat:aaa", "hello", SENDER)["reply"] == "pong"


def test_live_send_signs_with_the_sender(delivered):
    assert router.send("claude-chat:aaa", "hello", None, "macbook/claude-desktop")["from"] == "macbook/claude-desktop"


def test_live_send_refuses_fyi_to_a_chat(delivered):
    with pytest.raises(router.LiveError, match="no feed"):
        router.send("claude-chat:aaa", "hello", SENDER, fyi=True)


def test_dry_run_never_posts(cache, monkeypatch, delivered):
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: None)
    CliRunner().invoke(cloud_send.send_command("claude", "claude-chat"), ["aaa", "hello"])
    assert delivered == []


def test_dry_run_shows_the_target_chat(cache, monkeypatch):
    monkeypatch.setattr(cloud_send, "cache_root", lambda: cache)
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: None)
    result = CliRunner().invoke(cloud_send.send_command("claude", "claude-chat"), ["aaa", "hello"])
    assert "Trip planning  (Personal)" in result.output


def test_send_with_yes_prints_the_reply(cache, monkeypatch, delivered):
    monkeypatch.setattr("one_conv.live.router.whoami", lambda _: None)
    result = CliRunner().invoke(cloud_send.send_command("claude", "claude-chat"), ["aaa", "hello", "--yes"])
    assert result.stdout.strip() == "pong"
