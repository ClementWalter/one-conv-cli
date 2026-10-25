"""`one-conv live serve`: run, inspect or stop this machine's bridge to a live hub."""

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
    @click.option("--hub", help="https://HOST, http://127.0.0.1:PORT (loopback only), or ssh://[user@]HOST to tunnel to it.")
    @click.option("--node", help="This machine's name on the hub, as its token was issued (a-z, 0-9, -).")
    @click.option("--hub-port", type=int, help="The hub's port on an ssh host.  [default: 8789]")
    @click.option("--token-stdin", is_flag=True, help="Read this node's token from stdin and remember it (mode 600).")
    @click.option("--status", "show_status", is_flag=True, help="Show whether the bridge runs, and its last state.")
    @click.option("--stop", "stop_it", is_flag=True, help="Stop the running bridge.")
    @click.option("--install", "install_it", is_flag=True,
                  help="Run the bridge as a macOS login service: it starts at login and restarts after a crash.")
    @click.option("--uninstall", "uninstall_it", is_flag=True, help="Remove the login service.")
    @click.option("--yes", is_flag=True, help="Apply --install or --uninstall; without it, only show what would change.")
    @click.option("--json", "as_json", is_flag=True, help="Emit --status as JSON.")
    def serve_command(hub, node, hub_port, token_stdin, show_status, stop_it, install_it, uninstall_it, yes, as_json):
        """Connect this machine to a live hub, so its agents and agents elsewhere reach each other.

        The bridge publishes the sessions running here, delivers messages the hub holds for
        them through their usual adapters, and posts what `live send` queued while the hub
        was unreachable. No inbound network access is required. Run one per machine, in
        the foreground, under tmux, or as a login service (--install); its token comes
        from `live-hub.py token add`.
        """
        if install_it or uninstall_it:
            if not yes:
                click.echo(f"would {'install' if install_it else 'remove'} the login service {service.plist_path()}"
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
            click.echo(f"  queued for the hub: {state['outbox']}")
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
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        click.echo(f"bridge for node {node} via {hub}; stop with Ctrl-C or `one-conv live serve --stop`", err=True)
        sys.exit(bridge.serve(hub, node, token, hub_port))

    @live.command("spawner", context_settings=HELP,
                  epilog="\b\nExamples:\n"
                         "  one-conv live spawner --harness claude --cwd ~/work --yes\n"
                         "  one-conv live spawner --harness claude --cwd ~/work --arg=--dangerously-skip-permissions --yes\n"
                         "  one-conv live spawner --harness claude --cwd ~/work --priority 10 --yes   # asked before others\n"
                         "  one-conv live spawner            # show the current settings\n"
                         "  one-conv live spawner --off --yes")
    @click.option("--harness", type=click.Choice(["claude", "codex", "omp"]), help="The agent this machine starts.")
    @click.option("--cwd", type=click.Path(exists=True, file_okay=False, resolve_path=True),
                  help="The folder sessions start in.")
    @click.option("--model", help="The model the started sessions use; the harness default otherwise.")
    @click.option("--arg", "args", multiple=True, help="An extra argument for the harness command (repeatable).")
    @click.option("--priority", type=click.IntRange(-1000, 1000), default=0, show_default=True,
                  help="Spawners with a higher priority are asked first; the next one takes over if it is offline or silent.")
    @click.option("--off", is_flag=True, help="Start no sessions for the hub any more.")
    @click.option("--yes", is_flag=True, help="Save the change; without it, only show what would be saved.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the settings as JSON.")
    def spawner_command(harness, cwd, model, args, priority, off, yes, as_json):
        """Let the hub start a session on this machine when a request finds none running.

        The running bridge reads these settings at its next publication and offers to start
        sessions. The hub asks the connected spawner with the highest --priority, and passes
        the request to the next one when it is offline, refuses, or its session does not ack
        in time. Before each start the spawner pulls its one-conv checkout and, when --cwd is
        not a repository, writes AGENTS.md there (how to use one-conv, git pull/push rules).
        Sessions start detached in tmux, unattended: --arg is how they get their permission
        settings.
        """
        if off:
            change = None
        elif harness or cwd:
            if not (harness and cwd):
                raise click.UsageError("Give both --harness and --cwd, or --off.")
            change = {"harness": harness, "cwd": cwd, "model": model, "args": list(args), "priority": priority}
        else:
            current = bridge.spawn_settings()
            click.echo(json.dumps(current) if as_json else
                       (f"starts {current['harness']} in {current['cwd']}"
                        + (f" with {' '.join(current['args'])}" if current.get("args") else "")
                        if current else "starts no sessions for the hub"))
            return
        if not yes:
            click.echo(f"would save: {json.dumps(change)}; add --yes")
            return
        bridge.save_spawn_settings(change)
        click.echo(json.dumps(change) if as_json else
                   ("this machine starts no sessions for the hub" if change is None
                    else f"this machine starts {harness} sessions in {cwd} when the hub asks"))
