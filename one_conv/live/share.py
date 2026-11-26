"""Session sharing: a link that lets someone chat with one agent session, and with nothing else.

`live share` (or the MCP tool share_session) asks the relay, with this machine's node token,
for a share bound to the calling session's address. The relay keeps a digest of the share's
secret; the link `https://<relay>/s/<id>#<secret>` carries the secret in its fragment, which
browsers never send, and the guest page passes it in a header. A guest's message reaches the
session as kind="guest" from `guest:<id>`; the session answers with send_message to that address.

A colleague's agent uses the same share through its MCP server, `<relay>/s/<id>/mcp`, with the
secret as bearer token (`setup` prints the Claude Code and Codex configuration). A colleague
with one-conv sends to the link itself (`Guest`): the message is posted and the answer read back
inline, like a `claude-chat:` target.
"""

import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from one_conv import USER_AGENT

from . import hubclient, invite
from .model import LiveSession

SHARE_ID = re.compile(r"[0-9a-f]{16}")
DEFAULT_TTL = "24h"
MAX_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_LABEL = "guest"
# A share link: the relay's base URL, /s/<id>, and the secret after `#`.
LINK = re.compile(r"(?P<base>https?://[^/#\s?]+(?:/[^#\s?]*?)?)/s/(?P<id>[0-9a-f]{16})#(?P<secret>[A-Za-z0-9_-]{16,256})")
# How long a send to a link waits for the session's answer, and each long-poll of the relay within it.
REPLY_WAIT = 600.0
POLL_WAIT = 25.0
SECRET_MARK = "[secret]"


class ShareError(Exception):
    """A share that cannot be made, listed or revoked."""


def parse_ttl(text: str) -> int:
    """Seconds in `90s`, `15m`, `24h`, `7d`, refused beyond 30 days."""
    try:
        seconds = invite.parse_ttl(text)
    except invite.InviteError as error:
        raise ShareError(str(error)) from None
    if not 60 <= seconds <= MAX_TTL_SECONDS:
        raise ShareError("a share lasts from 1 minute to 30 days")
    return seconds


def _hub() -> hubclient.HubClient:
    try:
        hub = hubclient.configured()
    except hubclient.HubRefused as error:
        raise ShareError(str(error)) from None
    if hub is None:
        raise ShareError("Sharing needs this machine's relay: run `one-conv live join` with an invite, "
                         "or `one-conv live serve --hub ...`.")
    return hub


def public_base(hub: hubclient.HubClient) -> tuple[str, str | None]:
    """(base URL for links, warning): the relay's https URL, or the loopback one with a warning."""
    from . import bridge
    for candidate in (hub.url, bridge.settings().get("hub")):
        if isinstance(candidate, str) and urlsplit(candidate).scheme == "https":
            return candidate.rstrip("/"), None
    return hub.url, (f"this machine reaches its relay at {hub.url}, which a guest's browser cannot open: "
                     "the link works only where that address leads to the relay")


def link(base: str, ident: str, secret: str) -> str:
    return f"{base}/s/{ident}#{secret}"


def session_address(caller: LiveSession | None, explicit: str | None) -> str:
    """The session to share: --session when given, else the calling agent's own address."""
    if explicit:
        return explicit
    if caller is None or caller.id.startswith("?"):
        raise ShareError("Not inside an identifiable agent session; pass --session ADDRESS.")
    return caller.address


def _call(method: str, path: str, body: dict | None = None) -> dict:
    hub = _hub()
    try:
        return hub.request(method, path, body)
    except hubclient.HubRefused as error:
        if error.status == 404 and method == "POST" and path == "/v1/shares":
            raise ShareError("this relay does not share sessions yet; update it") from None
        raise ShareError(str(error)) from None
    except hubclient.HubUnavailable as error:
        raise ShareError(str(error)) from None


def create(address: str, label: str, ttl: int) -> dict:
    """{id, link, address, label, expires, warning?} of a new share; the link carries the secret once."""
    made = _call("POST", "/v1/shares", {"address": address, "label": label, "ttl": ttl})
    base, warning = public_base(_hub())
    shared = link(base, made["id"], made["secret"])
    result = {"id": made["id"], "link": shared, "address": made["address"], "label": made["label"],
              "expires": made["expires"], **setup(shared, made["label"])}
    return {**result, "warning": warning} if warning else result


def listing() -> list[dict]:
    """This machine's shares, newest first."""
    return _call("GET", "/v1/shares").get("shares", [])


def revoke(ident: str) -> dict:
    """End a share at once; its page then says the link is no longer active."""
    if not SHARE_ID.fullmatch(ident):
        raise ShareError(f"a share id is 16 lowercase hex characters, as `live share --list` shows, not {ident!r}")
    return _call("DELETE", f"/v1/shares/{ident}")


def server_name(label: str, ident: str) -> str:
    """The MCP server name a colleague's agent registers the share under: one-conv-share-<label>."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:32]
    return f"one-conv-share-{slug or ident[:8]}"


def setup(link_text: str, label: str) -> dict:
    """What a colleague needs, each carrying the secret: the MCP URL, Claude Code and Codex setup, a one-conv send."""
    base, ident, secret = parse_link(link_text)
    url, name = f"{base}/s/{ident}/mcp", server_name(label, ident)
    return {
        "mcp_url": url,
        "secret": secret,
        "claude_command": f'claude mcp add --transport http {name} {url} --header "Authorization: Bearer {secret}"',
        "codex_config": f'[mcp_servers.{name}]\nurl = "{url}"\nhttp_headers = {{ Authorization = "Bearer {secret}" }}',
        "one_conv_command": f"one-conv live send '{link_text}' 'hello' --yes",
    }


def is_link(target: str) -> bool:
    return bool(LINK.fullmatch(target.strip()))


def parse_link(text: str) -> tuple[str, str, str]:
    """(relay base URL, share id, secret) of a share link."""
    match = LINK.fullmatch(text.strip())
    if not match:
        raise ShareError("a share link is https://<relay>/s/<id>#<secret>, exactly as `live share` printed it")
    try:
        base = hubclient.check_url(match["base"])
    except ValueError as error:
        raise ShareError(f"share link: {error}") from None
    return base, match["id"], match["secret"]


def redacted(text: str) -> str:
    """A share link without its secret, safe to print and log."""
    base, ident, _ = parse_link(text)
    return f"{base}/s/{ident}"


class Guest:
    """One share, used as its guest: post to the shared session and read its answers.

    The secret goes only in the X-Share-Secret header, over a direct connection: proxies from
    the environment are ignored and a redirect is refused, as for the relay's node token.
    """

    def __init__(self, link_text: str):
        self.base, self.ident, self.secret = parse_link(link_text)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), hubclient._NoRedirect)

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Share-Secret": self.secret, "User-Agent": USER_AGENT,
                   **({"Content-Type": "application/json"} if data is not None else {})}
        request = urllib.request.Request(f"{self.base}/v1/shares/{self.ident}{path}", data=data, method=method,
                                         headers=headers)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error", "")
            except ValueError:
                message = error.reason
            raise ShareError(f"the share refused ({error.code}): {message}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ShareError(f"the relay at {self.base} is unreachable: {error}") from None

    def post(self, text: str) -> int:
        """Send text to the shared session; returns its seq in the exchange, the cursor for its answer."""
        return self._request("POST", "/messages", {"text": text})["seq"]

    def replies(self, after: int, seconds: float = 0.0) -> list[dict]:
        """The session's answers after `after`, waiting up to `seconds` for the first one."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            wait = min(POLL_WAIT, max(0.0, deadline - time.monotonic()))
            view = self._request("GET", f"/messages?after={after}&wait={wait:g}", timeout=wait + 30)
            replies = [m for m in view.get("messages", []) if m.get("from") == "agent" and m.get("seq", 0) > after]
            if replies or time.monotonic() >= deadline:
                return replies
