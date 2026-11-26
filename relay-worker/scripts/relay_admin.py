#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Manage the Cloudflare relay's node tokens through its admin API.

The admin secret is the Worker's RELAY_ADMIN_TOKEN (`wrangler secret put RELAY_ADMIN_TOKEN`);
this script reads it from the environment only, so it never shows up in argv or shell history.

    RELAY_ADMIN_TOKEN=... relay_admin.py --url https://relay.example.workers.dev token add NODE
    RELAY_ADMIN_TOKEN=... relay_admin.py --url ... token revoke NODE
    RELAY_ADMIN_TOKEN=... relay_admin.py --url ... token list
    RELAY_ADMIN_TOKEN=... relay_admin.py --url ... invite NODE [--ttl 15m] [--replace]

`token add` writes the new token to stdout once; hand it to that node's bridge
(`one-conv live serve --hub URL --node NODE --token-stdin`). Adding a node again replaces its token.
`invite` writes the one `one-conv live join ...` command to run on the new machine instead: a
one-time code, so no token is copied by hand. A connected machine makes the same invite with
`one-conv live invite NODE`, without the admin secret.
"""

import argparse
import json
import logging
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from one_conv.live import invite  # noqa: E402  (stdlib only: the checkout is the dependency)

log = logging.getLogger("relay-admin")


def call(url: str, admin: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """(status, JSON body) of one admin request; HTTP errors are returned, not raised."""
    request = Request(url.rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {admin}", "Content-Type": "application/json",
                               # Cloudflare refuses Python's default "Python-urllib" agent (error 1010).
                               "User-Agent": "one-conv-admin"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {"error": error.reason}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("ONE_CONV_RELAY_URL"), help="The Worker's URL [env: ONE_CONV_RELAY_URL].")
    commands = parser.add_subparsers(dest="command", required=True)
    token = commands.add_parser("token", help="Add, revoke or list node tokens.")
    token.add_argument("action", choices=("add", "revoke", "list"))
    token.add_argument("node", nargs="?")
    invitation = commands.add_parser("invite", help="Print a one-time `one-conv live join` command for a node.")
    invitation.add_argument("node")
    invitation.add_argument("--ttl", default=invite.DEFAULT_TTL, help="How long the code stays valid [default: 15m].")
    invitation.add_argument("--replace", action="store_true", help="Allow a node that already has a token.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    admin = os.environ.get("RELAY_ADMIN_TOKEN")
    if not args.url or not admin:
        log.error("set --url (or ONE_CONV_RELAY_URL) and RELAY_ADMIN_TOKEN")
        return 2
    if args.command == "token" and args.action != "list" and not args.node:
        log.error("token %s needs a NODE", args.action)
        return 2
    try:
        if args.command == "invite":
            try:
                ttl = invite.parse_ttl(args.ttl)
            except invite.InviteError as error:
                log.error("%s", error)
                return 2
            status, body = call(args.url, admin, "POST", "/admin/invites", {"node": args.node, "ttl": ttl, "replace": args.replace})
            if status == 200:
                # The only time the code exists in clear: it goes to stdout for the operator to hand over.
                sys.stdout.write(invite.join_command(args.url.rstrip("/"), body["node"], body["code"]) + "\n")
                return 0
        elif args.action == "add":
            status, body = call(args.url, admin, "POST", "/admin/tokens", {"node": args.node})
            if status == 200:
                # The only time the token exists in clear: it goes to stdout for the operator to hand over.
                sys.stdout.write(body["token"] + "\n")
                return 0
        elif args.action == "revoke":
            status, body = call(args.url, admin, "DELETE", f"/admin/tokens/{quote(args.node, safe='')}")
            if status == 200:
                return 0
        else:
            status, body = call(args.url, admin, "GET", "/admin/tokens")
            if status == 200:
                sys.stdout.write(json.dumps(body["tokens"], indent=2) + "\n")
                return 0
    except (URLError, OSError) as error:
        log.error("relay unreachable: %s", error)
        return 1
    log.error("relay refused (%d): %s", status, body.get("error"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
