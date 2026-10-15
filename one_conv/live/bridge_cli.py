"""`one-conv live serve`: run, inspect or stop this machine's bridge to a live hub."""

import json
import logging
import os
import sys

import click

from . import bridge, hubclient, tunnel

HELP = {"help_option_names": ["-h", "--help"]}


def _check_hub(hub: str) -> str:
    try:
        return hub if hub.startswith("ssh://") and tunnel.destination(hub) else hubclient.check_url(hub)
    except (ValueError, tunnel.TunnelError) as error:
        raise click.UsageError(f"--hub: {error}") from None


def add_bridge_commands(live: click.Group) -> None:
    @live.command("serve", context_settings=HELP,
                  epilog="\b\nExamples:\n"
                         "  one-conv live serve --hub ssh://me@hub-box --node laptop --token-stdin < token\n"
                         "  one-conv live serve --hub https://hub.example.com --node ci-runner\n"
                         "  one-conv live serve            # again, with the last settings\n"
                         "  one-conv live serve --status\n"
                         "  one-conv live serve --stop")
    @click.option("--hub", help="https://HOST, http://127.0.0.1:PORT, or ssh://[user@]HOST to tunnel to it.")
    @click.option("--node", help="This machine's name on the hub, as its token was issued (a-z, 0-9, -).")
    @click.option("--hub-port", type=int, help="The hub's port on an ssh host.  [default: 8789]")
    @click.option("--token-stdin", is_flag=True, help="Read this node's token from stdin and remember it (mode 600).")
    @click.option("--status", "show_status", is_flag=True, help="Show whether the bridge runs, and its last state.")
    @click.option("--stop", "stop_it", is_flag=True, help="Stop the running bridge.")
    @click.option("--json", "as_json", is_flag=True, help="Emit --status as JSON.")
    def serve_command(hub, node, hub_port, token_stdin, show_status, stop_it, as_json):
        """Connect this machine to a live hub, so its agents and agents elsewhere reach each other.

        The bridge publishes the sessions running here, delivers messages the hub holds for
        them through their usual adapters, and posts what `live send` queued while the hub
        was unreachable. It only connects out: nothing listens on this machine. Run one per
        machine, in the foreground or under tmux; its token comes from `live-hub.py token add`.
        """
        if show_status:
            state = bridge.status()
            if as_json:
                click.echo(json.dumps(state))
                return
            click.echo(f"{'running' if state['running'] else 'not running'}: node {state.get('node', '?')} "
                       f"via {state.get('hub', '?')}")
            if state.get("last_error"):
                click.echo(f"  last error: {state['last_error']}")
            click.echo(f"  queued for the hub: {state['outbox']}")
            for note in state.get("notes") or []:
                click.echo(f"  {note}")
            return
        if stop_it:
            click.echo("stopping the bridge" if bridge.stop() else "no bridge is running")
            return
        saved = bridge.settings()
        hub, node = hub or saved.get("hub"), node or saved.get("node")
        hub_port = hub_port or saved.get("hub_port") or 8789
        token = sys.stdin.read().strip() if token_stdin else os.environ.get("ONE_CONV_HUB_TOKEN") or saved.get("token")
        if not (hub and node and token):
            raise click.UsageError("Give --hub, --node and a token (--token-stdin or ONE_CONV_HUB_TOKEN) the first time.")
        if hub != saved.get("hub") and not (token_stdin or os.environ.get("ONE_CONV_HUB_TOKEN")):
            # A remembered token belongs to the remembered hub; never send it to another one.
            raise click.UsageError("A new --hub needs its own token: pass --token-stdin or ONE_CONV_HUB_TOKEN.")
        hub = _check_hub(hub)
        bridge.save_settings(hub, node, hub_port, token)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        click.echo(f"bridge for node {node} via {hub}; stop with Ctrl-C or `one-conv live serve --stop`", err=True)
        sys.exit(bridge.serve(hub, node, token, hub_port))
