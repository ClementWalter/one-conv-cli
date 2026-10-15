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

from . import tunnel
from .model import live_home

try:
    import fcntl
except ImportError:  # Windows has no flock; the outbox is then written unlocked.
    fcntl = None


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


# Statuses that say nothing final about the message: it stays queued under its id. A rejected
# token (401) is about the credential, so the message waits for the rotated one.
RETRYABLE = {401, 408, 425, 429}


def _receipt_for(receipt, message_id: str) -> bool:
    """A receipt names this id and lists recipients whose fields callers read; an empty list is valid."""
    if not (isinstance(receipt, dict) and receipt.get("id") == message_id
            and isinstance(receipt.get("recipients"), list)):
        return False
    return all(isinstance(row, dict) and isinstance(row.get("to"), str) and isinstance(row.get("status"), str)
               for row in receipt["recipients"])


def submit(hub: HubClient, message: dict) -> dict | None:
    """Post a message so that a crash or an ambiguous answer can neither lose it nor double it.

    The message is written to the outbox before any network I/O. It is removed only on a
    receipt for this id, or on a definitive refusal (a 4xx other than 401/408/425/429), which
    is raised. An unreachable hub, a 5xx, a rate limit or an unreadable receipt leaves it
    queued under the same id for the bridge to repost, and returns None.
    """
    path = queue(message)
    state, detail = _outcome(hub, message)
    if state == "retry":
        return None  # The hub may or may not have stored it; only a retry under this id can tell.
    path.unlink(missing_ok=True)
    if state == "refused":
        raise detail
    return detail


def _outcome(hub: HubClient, message: dict) -> tuple[str, object]:
    """("sent", receipt), ("refused", error) for a definitive 4xx, or ("retry", reason)."""
    try:
        receipt = hub.post(message)
    except HubUnavailable as error:
        return "retry", error
    except HubRefused as error:
        # Only a 4xx is the hub's verdict on this message; a redirect or 5xx may follow a commit.
        if 400 <= error.status < 500 and error.status not in RETRYABLE:
            return "refused", error
        return "retry", error
    if not _receipt_for(receipt, message["id"]):
        return "retry", "the hub's answer is not a receipt for this message"
    return "sent", receipt


def drain(hub: HubClient) -> dict:
    """Post queued messages oldest first, under the policy submit() uses.

    A receipt for the id removes the message; a definitive refusal moves it to
    outbox/refused/ for a person to inspect; anything ambiguous stops the drain and
    keeps the rest queued under their ids, so the order is kept for the next attempt.
    """
    sent, refused, waiting = [], [], []
    # A sender's own submit() can remove a file at any moment, so each step tolerates its absence.
    stamped = []
    for path in outbox().glob("*.json"):
        try:
            stamped.append((path.stat().st_mtime_ns, path.name, path))
        except FileNotFoundError:
            continue
    files = [path for _, _, path in sorted(stamped)]
    for index, path in enumerate(files):
        try:
            message = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            continue  # Unreadable files stay for a person to look at; they never block the rest.
        state, detail = _outcome(hub, message)
        if state == "sent":
            path.unlink(missing_ok=True)
            sent.append(message["id"])
        elif state == "refused":
            (outbox() / "refused").mkdir(exist_ok=True)
            try:
                os.replace(path, outbox() / "refused" / path.name)
            except FileNotFoundError:
                pass
            refused.append({"id": message["id"], "error": str(detail)})
        else:
            waiting = [p.stem for p in files[index:] if p.exists()]
            break
    return {"sent": sent, "refused": refused, "waiting": waiting}


class PendingConflict(Exception):
    """A different message is already waiting under this id."""


def queue(message: dict) -> Path:
    """Leave a message for the bridge to post once the hub is reachable; its id makes retries safe.

    A message already waiting under the same id is kept: an identical one reuses it, a
    different one raises PendingConflict, so a reused id never replaces pending work.
    """
    outbox().mkdir(parents=True, exist_ok=True)
    path = outbox() / f"{message['id']}.json"
    with (outbox() / ".lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            try:
                waiting = json.loads(path.read_text())
            except (OSError, ValueError):
                waiting = None
            if waiting == message:
                return path
            raise PendingConflict(f"message {message['id']} is already waiting with different content; "
                                  "use a new id for a new message")
        partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            handle.write(json.dumps(message))
        os.replace(partial, path)
    return path
