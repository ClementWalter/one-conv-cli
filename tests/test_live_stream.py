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
"""The bridge's stream client against relays that do not serve /v1/stream: it falls back to long-polling.

The Worker side of the stream (pushes, reconnects, revocation) is in test_relay_worker_stream.py.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from one_conv.live import bridge, hubclient
from one_conv.live.stream import InboxStream, StreamUnsupported, stream_url
from test_live_bridge import delivered, hub, outcome, post, short_poll  # noqa: F401  (fixtures)


@pytest.fixture
def answering(request):
    """A loopback server answering every request, a WebSocket upgrade included, with one status."""
    status = request.param

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # websockets reads only HTTP/1.1 answers, as every relay sends.

        def do_GET(self):
            payload = json.dumps({"error": "stub"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hubclient.HubClient(f"http://127.0.0.1:{server.server_address[1]}", "token", "box")
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(("url", "expected"), [
    ("https://relay.example.com", "wss://relay.example.com/v1/stream"),
    ("https://relay.example.com/prefix/", "wss://relay.example.com/prefix/v1/stream"),
    ("http://127.0.0.1:8789", "ws://127.0.0.1:8789/v1/stream"),
    ("http://[::1]:8789", "ws://[::1]:8789/v1/stream"),
])
def test_the_stream_url_keeps_the_relays_host_and_security(url, expected):
    assert stream_url(url) == expected


@pytest.mark.parametrize("answering", [403, 404, 405, 426, 501], indirect=True)
def test_an_upgrade_refused_as_unsupported_falls_back(answering):
    with pytest.raises(StreamUnsupported):
        InboxStream.open(answering)


@pytest.mark.parametrize("answering", [401], indirect=True)
def test_an_upgrade_refused_for_the_token_is_a_401(answering):
    with pytest.raises(hubclient.HubRefused, match=r"\(401\)"):
        InboxStream.open(answering)


@pytest.mark.parametrize("answering", [500, 502, 503], indirect=True)
def test_an_upgrade_failing_on_the_server_is_retried_not_dropped(answering):
    with pytest.raises(hubclient.HubUnavailable):
        InboxStream.open(answering)


def test_an_unreachable_relay_is_unavailable():
    with pytest.raises(hubclient.HubUnavailable):
        InboxStream.open(hubclient.HubClient("http://127.0.0.1:9", "token", "box"), timeout=2)


def test_the_python_relay_has_no_stream(hub):
    with pytest.raises(StreamUnsupported):
        InboxStream.open(hubclient.HubClient(hub.url, hub.box_token, "box"))


@pytest.fixture
def served_by_python_relay(hub, delivered):
    """A real bridge on the Python relay that has delivered one message; yields (hub, message)."""
    done = threading.Event()
    thread = threading.Thread(target=bridge.serve, args=(hub.url, "box", hub.box_token),
                              kwargs={"stopping": done.is_set}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while "box/omp:worker-1" not in {s["address"] for s in hub.mac.request("GET", "/v1/sessions")["sessions"]}:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    message = post(hub)
    while outcome(hub, message) == "stored":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    done.set()
    thread.join(timeout=15)
    return hub, message


def test_a_bridge_on_the_python_relay_long_polls(served_by_python_relay):
    assert bridge.status()["transport"] == "long-poll"


def test_a_bridge_on_the_python_relay_still_delivers(served_by_python_relay):
    assert outcome(*served_by_python_relay) == "accepted"
