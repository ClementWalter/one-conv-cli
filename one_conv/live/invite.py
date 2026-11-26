"""Invites: how a connected machine lets a new one join the relay without handing over any secret.

`live invite NODE` asks the relay, with this machine's node token, for a one-time code bound
to NODE. It prints `one-conv live join <hub>#<node>.<code>`: the part after `#` is a URL
fragment, which no HTTP client sends, and the code only ever travels in a POST body.
`live join` trades the code for NODE's token (POST /v1/join) and sets the machine up.
"""

import json
import re
import shlex
import urllib.error
import urllib.request
from contextlib import contextmanager

from one_conv import USER_AGENT

from . import hubclient, tunnel

DEFAULT_TTL = "15m"
DEFAULT_HUB_PORT = 8789
TTL = re.compile(r"(\d+)\s*([smhd]?)")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
# A code is unpadded base64url; a node name is lowercase letters, digits and dashes.
INVITE = re.compile(r"(?P<hub>[^#\s]+)#(?P<node>[a-z0-9][a-z0-9-]{0,31})\.(?P<code>[A-Za-z0-9_-]{16,256})")


class InviteError(Exception):
    """An invite that cannot be made, read or redeemed."""


def parse_ttl(text: str) -> int:
    """Seconds in `90s`, `15m`, `2h`, `1d` or a bare number of seconds."""
    match = TTL.fullmatch(text.strip().lower())
    if not match:
        raise InviteError(f"a duration is a number with s, m, h or d, like 15m, not {text!r}")
    return int(match.group(1)) * UNITS[match.group(2)]


def format_invite(hub: str, node: str, code: str) -> str:
    return f"{hub}#{node}.{code}"


def parse_invite(text: str) -> tuple[str, str, str]:
    """(hub, node, code) of what `live invite` printed."""
    match = INVITE.fullmatch(text.strip())
    if not match:
        raise InviteError("an invite is HUB#NODE.CODE, exactly as `one-conv live invite` printed it")
    return match["hub"], match["node"], match["code"]


def join_command(hub: str, node: str, code: str, hub_port: int = DEFAULT_HUB_PORT) -> str:
    """The one command to paste on the new machine."""
    port = f" --hub-port {hub_port}" if hub.startswith("ssh://") and hub_port != DEFAULT_HUB_PORT else ""
    # Quoted: `#` is a glob operator under zsh extendedglob.
    return f"one-conv live join {shlex.quote(format_invite(hub, node, code))}{port} --yes"


@contextmanager
def reach(hub: str, hub_port: int = DEFAULT_HUB_PORT):
    """The relay's base URL: itself for https, or a short-lived ssh forward for ssh://."""
    if not hub.startswith("ssh://"):
        yield hubclient.check_url(hub)
        return
    forward = tunnel.Tunnel(tunnel.destination(hub), hub_port)
    try:
        yield forward.open()
    finally:
        forward.close()


def _post(url: str, path: str, body: dict, token: str | None = None) -> dict:
    """POST JSON without following redirects or proxies, like HubClient; errors raise InviteError."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), hubclient._NoRedirect)
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get("error", "")
        except ValueError:
            message = error.reason
        if error.code == 404 and not message:
            message = "this relay does not issue invites; update it"
        raise InviteError(f"the relay refused ({error.code}): {message}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise InviteError(f"the relay at {url} is unreachable: {error}") from None


def create(url: str, token: str, node: str, ttl: int, replace: bool) -> dict:
    """{node, code, expires} of a new invite, made with this machine's node token."""
    return _post(url, "/v1/invites", {"node": node, "ttl": ttl, "replace": replace}, token)


def redeem(url: str, code: str) -> dict:
    """{node, token} for an invite code; the code is spent."""
    answer = _post(url, "/v1/join", {"code": code})
    if not (isinstance(answer.get("node"), str) and isinstance(answer.get("token"), str)):
        raise InviteError("the relay's answer carries no node token")
    return answer
