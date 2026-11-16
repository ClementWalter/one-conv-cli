"""The bridge's inbox as a WebSocket (GET /v1/stream) instead of /v1/inbox long-polls.

The relay pushes each delivery without an outcome as a frame `{"messages": [delivery]}`, the
/v1/inbox body, when the socket opens and whenever a send creates one. The bridge still acks
over POST /v1/acks, so a delivery whose ack never landed is pushed again on the next connection
and the ingress log keeps it from reaching the agent twice.

The socket follows HubClient's rules: the token goes only to the configured URL over a direct
TCP connection (no proxy from the environment, no redirect), with one-conv's User-Agent, and
through an ssh forward only while the bridge's own ssh holds the port. Keepalive is a "ping"
text frame the Worker's runtime answers with "pong" without waking the relay's Durable Object,
which is why protocol-level pings are turned off.
"""

import json
import logging
import socket
import time
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from one_conv import USER_AGENT

from . import tunnel
from .hubclient import HubClient, HubRefused, HubUnavailable

logger = logging.getLogger(__name__)

PING = "ping"
PONG = "pong"
PING_SECONDS = 20.0
# A pong slower than this means the connection is gone even if TCP has not noticed.
PONG_SECONDS = 20.0
# The relay closes a socket with this code when its token is revoked or replaced.
REVOKED = 4401
# One delivery per frame: 200,000 characters JSON-escaped, plus attachment metadata.
MAX_FRAME = 4 * 1024 * 1024
# Upgrade answers that mean "no stream here": long-polling /v1/inbox still works there.
# The Python relay answers a WebSocket upgrade with 403 (uvicorn) or 404 (no such route).
UNSUPPORTED = {403, 404, 405, 426, 501}


class StreamUnsupported(Exception):
    """The relay does not serve /v1/stream; the bridge long-polls /v1/inbox instead."""

    def __init__(self, status: int):
        super().__init__(f"the relay answered the stream upgrade with HTTP {status}")
        self.status = status


def stream_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}{parts.path.rstrip('/')}/v1/stream"


class InboxStream:
    """One open /v1/stream connection; `receive` returns deliveries as /v1/inbox would."""

    def __init__(self, connection: ClientConnection):
        self.connection = connection
        self.ping_sent: float | None = None
        self.last_heard = time.monotonic()

    @classmethod
    def open(cls, client: HubClient, timeout: float = 15) -> "InboxStream":
        """Connect, or raise StreamUnsupported, HubRefused (401) or HubUnavailable."""
        parts = urlsplit(client.url)
        if client.listener_pid is not None and not tunnel.listening(client.listener_pid, parts.port or 0):
            raise HubUnavailable("the bridge's ssh forward is not running")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            # A socket made here never goes through a proxy, and websockets refuses redirects on it.
            sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        except OSError as error:
            raise HubUnavailable(f"hub at {client.url} is unreachable: {error}") from None
        try:
            connection = connect(stream_url(client.url), sock=sock, additional_headers={"Authorization": f"Bearer {client.token}"},
                                 user_agent_header=USER_AGENT, proxy=None, open_timeout=timeout, ping_interval=None,
                                 max_size=MAX_FRAME, compression=None, close_timeout=2, legacy=True)
        except InvalidStatus as error:
            sock.close()
            status = error.response.status_code
            if status == 401:
                raise HubRefused(401, "unknown or revoked token") from None
            if status in UNSUPPORTED:
                raise StreamUnsupported(status) from None
            raise HubUnavailable(f"the stream upgrade failed with HTTP {status}") from None
        except (InvalidHandshake, OSError, TimeoutError, ValueError) as error:
            sock.close()
            raise HubUnavailable(f"the stream to {client.url} did not open: {error}") from None
        return cls(connection)

    def receive(self, wait: float) -> list[dict]:
        """Deliveries that arrived, returned as soon as one does or after `wait` seconds with none.

        A drop, a close or a missing pong raises HubUnavailable; a close for a revoked token
        raises HubRefused(401), as an HTTP request with it would.
        """
        deadline = time.monotonic() + wait
        while True:
            now = time.monotonic()
            if self.ping_sent is None and now - self.last_heard >= PING_SECONDS:
                self._send(PING)
                self.ping_sent = now
            timeout = max(0.0, deadline - now)
            if self.ping_sent is not None:
                timeout = min(timeout, max(0.0, self.ping_sent + PONG_SECONDS - now))
            else:
                timeout = min(timeout, max(0.0, self.last_heard + PING_SECONDS - now))
            try:
                frame = self.connection.recv(timeout=timeout)
            except TimeoutError:
                # Only an empty buffer gets here, so a pong that arrived while the bridge was busy counts.
                if self.ping_sent is not None and time.monotonic() - self.ping_sent >= PONG_SECONDS:
                    raise HubUnavailable(f"no pong from the relay within {PONG_SECONDS:g}s") from None
                if time.monotonic() >= deadline:
                    return []
                continue
            except ConnectionClosed as closed:
                raise self._closed(closed) from None
            self.last_heard, self.ping_sent = time.monotonic(), None
            if frame == PONG:
                continue
            try:
                messages = json.loads(frame).get("messages")
            except (ValueError, AttributeError):
                logger.warning("ignoring a stream frame that is not a delivery: %.80r", frame)
                continue
            if isinstance(messages, list):
                return [item for item in messages if isinstance(item, dict) and isinstance(item.get("delivery"), str)]
            logger.warning("ignoring a stream frame without deliveries: %.80r", frame)

    def _send(self, text: str) -> None:
        try:
            self.connection.send(text)
        except ConnectionClosed as closed:
            raise self._closed(closed) from None

    @staticmethod
    def _closed(closed: ConnectionClosed) -> Exception:
        received = closed.rcvd
        if received is not None and received.code == REVOKED:
            return HubRefused(401, received.reason or "unknown or revoked token")
        return HubUnavailable(f"the relay stream closed: {closed}")

    def close(self) -> None:
        self.connection.close()
