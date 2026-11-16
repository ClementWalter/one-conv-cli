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
"""Relay and archive clients name themselves, since Cloudflare refuses Python's default agent (error 1010)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from one_conv import USER_AGENT
from one_conv.archive import ArchiveClient
from one_conv.live.hubclient import HubClient


class Recorder(BaseHTTPRequestHandler):
    """Answers every request with an empty JSON object and keeps the agents it was sent with."""

    agents: list[str] = []

    def _answer(self):
        Recorder.agents.append(self.headers.get("User-Agent", ""))
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"conversations": [], "next": None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.agents = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_relay_client_sends_the_one_conv_agent(server):
    HubClient(server, "token", "node").request("GET", "/v1/whoami")
    assert Recorder.agents == [USER_AGENT]


def test_archive_client_sends_the_one_conv_agent(server):
    ArchiveClient(server, "token").request("conversations")
    assert Recorder.agents == [USER_AGENT]
