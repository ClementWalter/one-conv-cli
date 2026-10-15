"""Client for the live hub, shared by the CLI and the per-machine bridge (`live serve`).

The bridge writes `<live_home>/bridge/hub.json` (mode 0600) with the hub URL it can reach
right now (a local ssh tunnel, or https), this machine's node name and its token.
ONE_CONV_HUB_URL with ONE_CONV_HUB_TOKEN selects another hub; ONE_CONV_HUB_TOKEN alone
rotates the token for the configured hub; a URL without a token is refused, so an old
token is never sent to a new endpoint. Without any of these there is no hub and topics
stay on this machine. The bearer token goes only to the configured URL, over a direct
connection: a redirect is refused, and proxies from the environment are ignored.
"""

import ipaddress
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import tunnel
from .model import live_home


class HubUnavailable(Exception):
    """The hub could not be reached; nothing was acknowledged."""


class HubRefused(Exception):
    """The hub answered with an error."""

    def __init__(self, status: int, message: str):
        super().__init__(f"hub refused ({status}): {message}")
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Following it would send the bearer token to wherever the redirect points.


def check_url(url: str) -> str:
    """Accept https, or plain http only to a literal loopback address such as an ssh forward.

    A URL carrying user information is refused: credentials belong in the token, never in
    an address that ends up in logs.
    """
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("a hub URL must not contain user information")
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname:
        try:
            if ipaddress.ip_address(parts.hostname).is_loopback:
                return url.rstrip("/")
        except ValueError:
            pass
        raise ValueError("plain http is allowed only to a loopback address (127.0.0.1, ::1); use https")
    raise ValueError(f"a hub URL is https://host or http://127.0.0.1:port, got {url!r}")


class HubClient:
    def __init__(self, url: str, token: str, node: str | None = None, timeout: float = 15,
                 listener_pid: int | None = None):
        self.url, self.token, self.node, self.timeout = check_url(url), token, node, timeout
        # The bridge's ssh process: a forwarded port is trusted only while that process holds it.
        self.listener_pid = listener_pid
        # An empty ProxyHandler keeps http_proxy/https_proxy from routing the token and body elsewhere.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.listener_pid is not None and not tunnel.listening(self.listener_pid, urlsplit(self.url).port or 0):
            # The bridge's forward is gone; whatever listens on that port now must not get the token.
            raise HubUnavailable("the bridge's ssh forward is not running")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Authorization": f"Bearer {self.token}"})
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error", "")
            except ValueError:
                message = error.reason
            raise HubRefused(error.code, message) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise HubUnavailable(f"hub at {self.url} is unreachable: {error}") from None

    # Topics ---------------------------------------------------------------------------------

    def join(self, topic: str, address: str) -> dict:
        return self.request("PUT", f"/v1/topics/{quote(topic)}/members/{quote(address, safe=':')}")

    def leave(self, topic: str, address: str) -> dict:
        return self.request("DELETE", f"/v1/topics/{quote(topic)}/members/{quote(address, safe=':')}")

    def topics(self) -> list[dict]:
        return self.request("GET", "/v1/topics").get("topics", [])

    def history(self, topic: str, after: str | None = None, limit: int = 100) -> list[dict]:
        query = f"?limit={limit}" + (f"&after={quote(after)}" if after else "")
        return self.request("GET", f"/v1/topics/{quote(topic)}/messages{query}").get("messages", [])

    def post(self, message: dict) -> dict:
        return self.request("POST", "/v1/messages", message)


def configured() -> HubClient | None:
    """The hub this machine reaches, or None when none is set up."""
    env_url, env_token = os.environ.get("ONE_CONV_HUB_URL"), os.environ.get("ONE_CONV_HUB_TOKEN")
    node = os.environ.get("ONE_CONV_HUB_NODE")
    if env_url and not env_token:
        raise HubRefused(0, "ONE_CONV_HUB_URL is set without ONE_CONV_HUB_TOKEN; set both to use another hub")
    url, token = env_url, env_token
    if not env_url:
        try:
            config = json.loads((live_home() / "bridge" / "hub.json").read_text())
        except (OSError, ValueError):
            config = None
        if not isinstance(config, dict):
            if env_token:
                raise HubRefused(0, "ONE_CONV_HUB_TOKEN is set but no hub URL is configured")
            return None
        url, node = config.get("url"), node or config.get("node")
        listener = config.get("tunnel_pid") if isinstance(config.get("tunnel_pid"), int) else None
        # A token on its own rotates the credential for the configured hub, nothing else.
        token = env_token or config.get("token")
    if not (isinstance(url, str) and isinstance(token, str) and url and token):
        return None
    try:
        return HubClient(url, token, node if isinstance(node, str) else None,
                         listener_pid=None if env_url else listener)
    except ValueError as error:
        raise HubRefused(0, f"configured hub URL rejected: {error}") from None


def outbox() -> Path:
    return live_home() / "bridge" / "outbox"


def is_terminal(error: HubRefused) -> bool:
    """Whether the hub would refuse this exact message again (malformed, forged or a conflicting id).

    A 5xx, a rate limit, a timeout or a rejected token says nothing final about the message.
    """
    return 400 <= error.status < 500 and error.status not in (401, 408, 425, 429)


def post_queued(client: HubClient, message: dict, path: Path) -> dict:
    """Post a message already in the outbox; its record goes only once the hub gave a receipt for its id.

    A terminal refusal is raised with the record left for the caller to drop or set aside;
    any other failure, or an answer that is not a receipt, raises HubUnavailable and keeps it.
    """
    try:
        receipt = client.post(message)
    except HubRefused as error:
        if is_terminal(error):
            raise
        raise HubUnavailable(f"the hub answered {error.status}; the message stays queued") from None
    if not (isinstance(receipt, dict) and receipt.get("id") == message.get("id")
            and isinstance(receipt.get("recipients"), list)):
        raise HubUnavailable("the hub's answer is not a receipt for this message; it stays queued")
    path.unlink(missing_ok=True)
    return receipt


def post_durably(client: HubClient, message: dict) -> dict:
    """Post `message`, with its outbox record written first so a crash mid-post leaves it to resume.

    On HubUnavailable the record stays, and the bridge posts it under the same id, which the
    hub answers with the first receipt.
    """
    path = queue(message)
    try:
        return post_queued(client, message, path)
    except HubRefused:
        path.unlink(missing_ok=True)
        raise


def queue(message: dict) -> Path:
    """Leave a message for the bridge to post; an id already queued keeps its first content.

    The record is created with link(), which fails if it exists, so concurrent writers of one
    id cannot replace each other's text.
    """
    outbox().mkdir(parents=True, exist_ok=True)
    path = outbox() / f"{message['id']}.json"
    partial = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(message, handle)
    try:
        for _ in range(3):
            try:
                os.link(partial, path)
                return path
            except FileExistsError:
                try:
                    existing = json.loads(path.read_text())
                except FileNotFoundError:
                    continue  # Posted and removed meanwhile: queue this one afresh.
                except (OSError, ValueError):
                    existing = None
                if existing == message:
                    return path
                raise HubRefused(409, f"message {message['id']} is already queued with different content; "
                                      "send it under a new id") from None
        raise HubUnavailable(f"could not queue message {message['id']}")
    finally:
        partial.unlink(missing_ok=True)
