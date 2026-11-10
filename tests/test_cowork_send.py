#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Sending into a Cowork session posts one user event and returns the turn's reply once it ends."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from one_conv.providers.base import JsonTransport, ProviderUnavailable, SchemaChanged
from one_conv.providers.cowork import CoworkProvider

SESSION = "cse_01Test"
EVENTS = f"/v1/code/sessions/{SESSION}/events"


def event(seq, kind, **payload):
    return {"event_id": f"e{seq}", "sequence_num": str(seq), "event_type": kind, "payload": payload}


def assistant(seq, text):
    return event(seq, "assistant", message={"role": "assistant", "content": [{"type": "text", "text": text}]})


EARLIER = [event(1, "user", message={"role": "user", "content": "old"}), assistant(2, "old reply"),
           event(3, "result", subtype="success")]


class StubTransport:
    """Acknowledge the post as sequence 4 and serve successive snapshots of the event log."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.posts = []

    def post_session_events(self, path, body):
        self.posts.append((path, body))
        return {"results": [{"event_id": "posted", "sequence_num": "4", "duplicate": False}]}

    def get(self, path, params=None):
        rows = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        return {"data": deepcopy(rows), "next_cursor": None}


def provider(transport):
    stub = CoworkProvider.__new__(CoworkProvider)
    stub.transport = transport
    return stub


FINISHED = EARLIER + [event(4, "user", message={"role": "user", "content": "hi"}), assistant(5, "reçu"),
                      event(6, "result", subtype="success")]


@pytest.fixture
def transport():
    return StubTransport([EARLIER, FINISHED])


def send(transport, **options):
    return provider(transport).send_message(SESSION, "hi", sleep=lambda _: None, **options)


def test_send_posts_one_user_event(transport):
    send(transport)
    assert transport.posts[0][1]["events"][0]["payload"]["message"] == {"role": "user", "content": "hi"}


def test_send_posts_to_the_session_events(transport):
    send(transport)
    assert transport.posts[0][0] == EVENTS


def test_send_ignores_replies_from_earlier_turns(transport):
    assert send(transport).reply == "reçu"


def test_send_reports_how_the_turn_ended(transport):
    assert send(transport).stop_reason == "success"


def test_send_waits_for_the_turn_to_end():
    pending = EARLIER + [event(4, "user", message={"role": "user", "content": "hi"}), assistant(5, "working")]
    transport = StubTransport([pending, pending, FINISHED])
    assert send(transport).reply == "reçu"


def test_send_times_out_with_the_message_posted():
    ticks = iter([0, 0, 700])
    with pytest.raises(ProviderUnavailable, match="message is posted"):
        send(StubTransport([EARLIER]), timeout=600, clock=lambda: next(ticks))


def test_send_rejects_an_unacknowledged_post():
    transport = StubTransport([FINISHED])
    transport.post_session_events = lambda path, body: {"results": []}
    with pytest.raises(SchemaChanged):
        send(transport)


class StubSession:
    def __init__(self, status):
        self.status, self.calls = status, []

    def post(self, url, **kwargs):
        self.calls.append(url)
        return SimpleNamespace(status_code=self.status, headers={}, json=lambda: {"results": []})


def test_events_route_is_the_only_cowork_write():
    with pytest.raises(ValueError):
        JsonTransport(StubSession(200), "https://claude.ai", "fixture").post_session_events("/v1/code/sessions", {})


def test_events_post_is_never_retried():
    session = StubSession(503)
    with pytest.raises(ProviderUnavailable):
        JsonTransport(session, "https://claude.ai", "fixture").post_session_events(EVENTS, {})
    assert len(session.calls) == 1


@pytest.fixture
def saved_session(tmp_path, monkeypatch):
    from one_conv import cloud_send
    import json
    (tmp_path / "account").mkdir()
    (tmp_path / "account" / "s.json").write_text(json.dumps({
        "source": "cowork-cloud", "id": SESSION, "session": "abc", "title": "Test", "account_id": "acct",
        "account_label": "Work"}))
    monkeypatch.setattr(cloud_send, "cache_root", lambda: tmp_path)
    calls = []

    class Provider:
        def send_message(self, conversation_id, text, **options):
            calls.append(options)
            return SimpleNamespace(message_id="posted", reply_id=None, reply="reçu", stop_reason="success")
    monkeypatch.setattr("one_conv.cloud_cli.browser_provider", lambda product, account, discover: Provider())
    return calls


def test_deliver_to_cowork_passes_no_chat_options(saved_session):
    from one_conv import cloud_send
    cloud_send.deliver("cowork-cloud", "Test", "hi", accounts=lambda: iter(()))
    assert saved_session == [{}]


def test_deliver_to_cowork_addresses_the_session(saved_session):
    from one_conv import cloud_send
    assert cloud_send.deliver("cowork-cloud", "Test", "hi", accounts=lambda: iter(()))["to"] == "cowork:" + SESSION
