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
from pathlib import Path
from urllib.parse import quote, urlsplit

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
    def __init__(self, url: str, token: str, node: str | None = None, timeout: float = 15):
        self.url, self.token, self.node, self.timeout = check_url(url), token, node, timeout
        # An empty ProxyHandler keeps http_proxy/https_proxy from routing the token and body elsewhere.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
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
        # A token on its own rotates the credential for the configured hub, nothing else.
        token = env_token or config.get("token")
    if not (isinstance(url, str) and isinstance(token, str) and url and token):
        return None
    try:
        return HubClient(url, token, node if isinstance(node, str) else None)
    except ValueError as error:
        raise HubRefused(0, f"configured hub URL rejected: {error}") from None


def outbox() -> Path:
    return live_home() / "bridge" / "outbox"


def submit(hub: HubClient, message: dict) -> dict | None:
    """Post a message so that a crash at any point can neither lose it nor post it twice.

    The message is written to the outbox before any network I/O and removed once the hub
    has answered, accepted or refused. If the process dies in between, the bridge reposts
    the outbox file under the same id and the hub returns the original receipt. Returns
    None while the hub is unreachable (the message stays queued); a refusal is raised.
    """
    path = queue(message)
    try:
        receipt = hub.post(message)
    except HubUnavailable:
        return None
    except HubRefused:
        path.unlink(missing_ok=True)
        raise
    path.unlink(missing_ok=True)
    return receipt


def queue(message: dict) -> Path:
    """Leave a message for the bridge to post once the hub is reachable; its id makes retries safe."""
    outbox().mkdir(parents=True, exist_ok=True)
    path = outbox() / f"{message['id']}.json"
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(message))
    os.replace(partial, path)
    return path
