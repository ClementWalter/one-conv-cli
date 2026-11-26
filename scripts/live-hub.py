#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Run the live relay, or manage its node tokens, on the machine that hosts it.

One process serves the relay API (direct messages between machines' bridges) and the
conversation archive every bridge syncs to, both authenticated by the same node tokens.

    live-hub.py serve --db relay.db [--archive-db archive.db] [--host 127.0.0.1] [--port 8789]
    live-hub.py token add NODE --db relay.db      # prints the token once; give it to that node's bridge
    live-hub.py token revoke NODE --db relay.db
    live-hub.py invite NODE --db relay.db --url https://relay.example.com [--ttl 15m] [--replace]

`invite` prints the one `one-conv live join ...` command to run on the new machine: a one-time
code, so no token is copied by hand. Machines already connected can make invites themselves
with `one-conv live invite NODE`.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from one_conv.live import invite
from one_conv.live.hub import Hub, HubError, application

log = logging.getLogger("live-hub")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the relay API and the conversation archive.")
    serve.add_argument("--db", required=True, help="SQLite file holding tokens, sessions and messages.")
    serve.add_argument("--archive-db", help="SQLite file holding the conversation archive "
                                            "[default: archive.db next to --db].")
    # Localhost by default: exposing the relay is a deliberate choice (TLS proxy, or ssh tunnels only).
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8789)
    token = commands.add_parser("token", help="Add or revoke a node's token.")
    token.add_argument("action", choices=("add", "revoke"))
    token.add_argument("node")
    token.add_argument("--db", required=True)
    invitation = commands.add_parser("invite", help="Print a one-time `one-conv live join` command for a node.")
    invitation.add_argument("node")
    invitation.add_argument("--db", required=True)
    invitation.add_argument("--url", required=True,
                            help="The relay as the new machine reaches it: https://HOST or ssh://[user@]HOST.")
    invitation.add_argument("--ttl", default=invite.DEFAULT_TTL, help="How long the code stays valid [default: 15m].")
    invitation.add_argument("--replace", action="store_true", help="Allow a node that already has a token.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    os.umask(0o077)  # The databases hold token hashes, message text and conversation history.
    hub = Hub(args.db)
    if args.command == "invite":
        try:
            made = hub.create_invite("", args.node, invite.parse_ttl(args.ttl), args.replace)
        except (HubError, invite.InviteError) as error:
            log.error("%s", error)
            return 1
        # The code exists in clear only here: it goes to stdout for the operator to hand over.
        sys.stdout.write(invite.join_command(args.url.rstrip("/"), made["node"], made["code"]) + "\n")
        return 0
    if args.command == "token":
        try:
            if args.action == "add":
                # The only time the token exists in clear: it goes to stdout for the operator to hand over.
                sys.stdout.write(hub.add_token(args.node) + "\n")
            elif not hub.revoke(args.node):
                log.error("no token for node %s", args.node)
                return 1
        except HubError as error:
            log.error("%s", error)
            return 1
        return 0
    import uvicorn
    archive_db = args.archive_db or str(Path(args.db).resolve().with_name("archive.db"))
    log.info("relay on %s:%d, archive in %s", args.host, args.port, archive_db)
    uvicorn.run(application(hub, archive_db), host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
