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
"""The bridge on the Worker relay's /v1/stream: pushes on connect and after, keepalive, revocation, reconnects.

Runs only with ONE_CONV_TEST_RELAY_WORKER=1 (`wrangler dev --local`, no Cloudflare account).
The adapter is the recording stub of test_live_bridge, so no real session is ever messaged.
"""

import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from one_conv.live import bridge, hubclient, stream
from one_conv.live.stream import InboxStream
from test_live_bridge import delivered, short_poll  # noqa: F401  (fixtures)

TARGET = "box/omp:worker-1"


@pytest.fixture
def relay(relay_worker):
    """The Worker emptied, with node `mac` (session claude:aaa) and a token for node `box`."""
    state = relay_worker.hub()
    state.publish("mac", [{"address": "claude:aaa", "name": "lead"}])
    box = state.add_token("box")
    state.publish("box", [{"address": "omp:worker-1"}])
    return SimpleNamespace(state=state, url=relay_worker.url, box=box,
                           client=hubclient.HubClient(relay_worker.url, box, "box"))


def send(relay, text="run the tests") -> dict:
    message = {"id": uuid.uuid4().hex, "to": TARGET, "from": "claude:aaa", "text": text}
    relay.state.send("mac", message)
    return message


def outcome(relay, message) -> str:
    return relay.state.status("mac", message["id"])["recipients"][0]["outcome"]


@pytest.fixture
def opened(relay):
    connection = InboxStream.open(relay.client)
    yield connection
    connection.close()


def test_a_delivery_stored_before_connecting_is_pushed_on_connect(relay):
    send(relay, "stored first")
    connection = InboxStream.open(relay.client)
    try:
        assert [item["text"] for item in connection.receive(5)] == ["stored first"]
    finally:
        connection.close()


def test_a_delivery_sent_after_connecting_is_pushed(relay, opened):
    send(relay, "sent later")
    assert [item["text"] for item in opened.receive(5)] == ["sent later"]


def test_a_pushed_delivery_arrives_within_a_second(relay, opened):
    started = time.monotonic()
    send(relay)
    opened.receive(5)
    assert time.monotonic() - started < 1


def test_nothing_pending_returns_nothing_after_the_wait(opened):
    assert opened.receive(0.3) == []


def test_a_ping_is_answered(monkeypatch, opened):
    monkeypatch.setattr(stream, "PING_SECONDS", 0.1)
    started = time.monotonic()
    opened.receive(1)
    assert opened.last_heard > started  # Only a pong is heard: nothing else was sent.


def test_revoking_the_token_ends_the_stream_as_a_401(relay, opened):
    relay.state.revoke("box")
    with pytest.raises(hubclient.HubRefused, match=r"\(401\)"):
        opened.receive(5)


def test_a_bridge_with_a_stream_delivers_to_the_adapter(relay, delivered, opened):
    node = bridge.Bridge(relay.client, "box", bridge.Ingress(bridge.bridge_dir() / "ingress.db"), opened)
    node.publish()
    send(relay)
    node.step(wait=5)
    assert len(delivered) == 1


def test_a_bridge_with_a_stream_acks_what_it_delivered(relay, delivered, opened):
    node = bridge.Bridge(relay.client, "box", bridge.Ingress(bridge.bridge_dir() / "ingress.db"), opened)
    node.publish()
    message = send(relay)
    node.step(wait=5)
    assert outcome(relay, message) == "accepted"


def until(check, what: str, seconds: float = 20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def reconnected(relay, delivered, monkeypatch):
    """A running bridge whose stream drops; a message is sent before it is allowed to reopen.

    Yields (relay, message) once the bridge is past the reopen.
    """
    opens, reopening, release = [], threading.Event(), threading.Event()
    real_open = InboxStream.open.__func__

    def gated_open(cls, client, timeout=15):
        if opens:
            reopening.set()
            release.wait(20)
        opens.append(real_open(cls, client, timeout))
        return opens[-1]
    monkeypatch.setattr(InboxStream, "open", classmethod(gated_open))
    done = threading.Event()
    thread = threading.Thread(target=bridge.serve, args=(relay.url, "box", relay.box),
                              kwargs={"stopping": done.is_set}, daemon=True)
    thread.start()
    until(lambda: bridge.status().get("transport") == "stream" and bridge.status().get("last_ok"), "the first stream")
    opens[0].connection.close()
    until(reopening.is_set, "the bridge to redial")
    message = send(relay, "sent while the stream was down")
    release.set()
    until(lambda: outcome(relay, message) != "stored", "the delivery after reconnecting")
    done.set()
    thread.join(timeout=15)
    yield relay, message


def test_a_bridge_receives_after_reconnecting_what_was_sent_while_down(reconnected, delivered):
    assert "sent while the stream was down" in delivered[0]


def test_a_bridge_acks_after_reconnecting_what_was_sent_while_down(reconnected):
    assert outcome(*reconnected) == "accepted"
