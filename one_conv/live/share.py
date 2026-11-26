"""Session sharing: a link that lets someone chat with one agent session, and with nothing else.

`live share` (or the MCP tool share_session) asks the relay, with this machine's node token,
for a share bound to the calling session's address. The relay keeps a digest of the share's
secret; the link `https://<relay>/s/<id>#<secret>` carries the secret in its fragment, which
browsers never send, and the guest page passes it in a header. A guest's message reaches the
session as kind="guest" from `guest:<id>`; the session answers with send_message to that address.
"""

import re
from urllib.parse import urlsplit

from . import hubclient, invite
from .model import LiveSession

SHARE_ID = re.compile(r"[0-9a-f]{16}")
DEFAULT_TTL = "24h"
MAX_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_LABEL = "guest"


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
    result = {"id": made["id"], "link": link(base, made["id"], made["secret"]), "address": made["address"],
              "label": made["label"], "expires": made["expires"]}
    return {**result, "warning": warning} if warning else result


def listing() -> list[dict]:
    """This machine's shares, newest first."""
    return _call("GET", "/v1/shares").get("shares", [])


def revoke(ident: str) -> dict:
    """End a share at once; its page then says the link is no longer active."""
    if not SHARE_ID.fullmatch(ident):
        raise ShareError(f"a share id is 16 lowercase hex characters, as `live share --list` shows, not {ident!r}")
    return _call("DELETE", f"/v1/shares/{ident}")
