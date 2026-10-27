#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Run the live hub, or manage its node tokens, on the machine that hosts it.

    live-hub.py serve --db hub.db [--host 127.0.0.1] [--port 8789]
    live-hub.py token add NODE --db hub.db      # prints the token once; give it to that node's bridge
    live-hub.py token revoke NODE --db hub.db
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from one_conv.live.hub import Hub, HubError, application

log = logging.getLogger("live-hub")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the hub API.")
    serve.add_argument("--db", required=True, help="SQLite file holding tokens, sessions and messages.")
    # Localhost by default: exposing the hub is a deliberate choice (TLS proxy, or ssh tunnels only).
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8789)
    token = commands.add_parser("token", help="Add or revoke a node's token.")
    token.add_argument("action", choices=("add", "revoke"))
    token.add_argument("node")
    token.add_argument("--db", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    os.umask(0o077)  # The database holds token hashes and message text.
    hub = Hub(args.db)
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
    uvicorn.run(application(hub), host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
