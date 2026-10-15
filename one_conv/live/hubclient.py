"""Client for the live hub, shared by the CLI and the per-machine bridge (`live serve`).

The bridge writes `<live_home>/bridge/hub.json` (mode 0600) with the hub URL it can reach
right now (a local ssh tunnel, or https), this machine's node name and its token;
ONE_CONV_HUB_URL and ONE_CONV_HUB_TOKEN override it. Without either there is no hub and
topics stay on this machine. The bearer token is sent only to the configured URL: a
redirect is refused rather than followed.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

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


class HubClient:
    def __init__(self, url: str, token: str, node: str | None = None, timeout: float = 15):
        self.url, self.token, self.node, self.timeout = url.rstrip("/"), token, node, timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

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
    url, token = os.environ.get("ONE_CONV_HUB_URL"), os.environ.get("ONE_CONV_HUB_TOKEN")
    node = os.environ.get("ONE_CONV_HUB_NODE")
    if not (url and token):
        try:
            config = json.loads((live_home() / "bridge" / "hub.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(config, dict):
            return None
        url, token, node = config.get("url"), config.get("token"), config.get("node")
    if not (isinstance(url, str) and isinstance(token, str) and url and token):
        return None
    return HubClient(url, token, node if isinstance(node, str) else None)


def outbox() -> Path:
    return live_home() / "bridge" / "outbox"


def queue(message: dict) -> Path:
    """Leave a message for the bridge to post once the hub is reachable; its id makes retries safe."""
    outbox().mkdir(parents=True, exist_ok=True)
    path = outbox() / f"{message['id']}.json"
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(message))
    os.replace(partial, path)
    return path
