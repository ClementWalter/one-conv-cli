"""`one-conv live serve`: run, inspect or stop this machine's bridge to a live relay."""

import json
import logging
import os
import sys

import click

from . import bridge, hubclient, service, tunnel

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
                         "  one-conv live serve --hub https://hub.example.com --node ci-runner --token-stdin < ci-runner.token\n"
                         "  one-conv live serve            # again, with the last settings\n"
                         "  one-conv live serve --status\n"
                         "  one-conv live serve --stop\n"
                         "  one-conv live serve --install --yes   # start at login, restart after a crash")
    @click.option("--hub", help="The relay: https://HOST, http://127.0.0.1:PORT (loopback only), or ssh://[user@]HOST to tunnel to it.")
    @click.option("--node", help="This machine's name on the relay, as its token was issued (a-z, 0-9, -).")
    @click.option("--hub-port", type=int, help="The relay's port on an ssh host.  [default: 8789]")
    @click.option("--token-stdin", is_flag=True, help="Read this node's token from stdin and remember it (mode 600).")
    @click.option("--status", "show_status", is_flag=True, help="Show whether the bridge runs, and its last state.")
    @click.option("--stop", "stop_it", is_flag=True, help="Stop the running bridge.")
    @click.option("--install", "install_it", is_flag=True,
                  help="Run the bridge as a login service (launchd on macOS, systemd on Linux): it starts on its own"
                       " and restarts after a crash.")
    @click.option("--uninstall", "uninstall_it", is_flag=True, help="Remove the login service.")
    @click.option("--yes", is_flag=True, help="Apply --install or --uninstall; without it, only show what would change.")
    @click.option("--json", "as_json", is_flag=True, help="Emit --status as JSON.")
    def serve_command(hub, node, hub_port, token_stdin, show_status, stop_it, install_it, uninstall_it, yes, as_json):
        """Connect this machine to a live relay, so its agents and agents elsewhere reach each other.

        The bridge publishes the sessions running here, delivers messages the relay holds for
        them through their usual adapters, posts what `live send` queued while the relay was
        unreachable, and syncs this machine's agent histories to the relay's archive so any
        machine can read and continue them as NODE/harness:id. No inbound network access is
        required. Run one per machine, in the foreground, under tmux, or as a login service
        (--install); its token comes from `scripts/live-hub.py token add`.
        """
        if install_it or uninstall_it:
            if not yes:
                click.echo(f"would {'install' if install_it else 'remove'} the login service {service.path()}"
                           + (" and stop a bridge running outside it" if install_it and not service.installed()
                              and bridge.status()["running"] else "") + "; add --yes")
                return
            try:
                notes = service.install() if install_it else service.uninstall()
            except RuntimeError as error:
                raise click.ClickException(str(error)) from None
            for note in notes:
                click.echo(note)
            return
        if show_status:
            state = bridge.status()
            if as_json:
                click.echo(json.dumps(state))
                return
            click.echo(f"{'running' if state['running'] else 'not running'}: node {state.get('node', '?')} "
                       f"via {state.get('hub', '?')}")
            if state.get("last_error"):
                click.echo(f"  last error: {state['last_error']}")
            click.echo(f"  queued for the relay: {state['outbox']}")
            click.echo(f"  starts at login: {'yes' if service.installed() else 'no (one-conv live serve --install --yes)'}")
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
        # force: the CLI configured WARNING at import, which would hide the bridge's transport and delivery lines.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", force=True)
        click.echo(f"bridge for node {node} via {hub}; stop with Ctrl-C or `one-conv live serve --stop`", err=True)
        sys.exit(bridge.serve(hub, node, token, hub_port))
