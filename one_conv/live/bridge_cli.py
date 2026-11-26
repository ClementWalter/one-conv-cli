"""`one-conv live serve`, `live invite` and `live join`: connect this machine to a live relay and run its bridge."""

import json
import logging
import os
import sys
import time

import click

from . import bridge, hubclient, invite, provision, service, tunnel
from . import install as installer

HELP = {"help_option_names": ["-h", "--help"]}
# How long `live join` waits for the freshly installed bridge to take its lock.
JOIN_WAIT_SECONDS = 10.0


def _check_hub(hub: str) -> str:
    try:
        return hub if hub.startswith("ssh://") and tunnel.destination(hub) else hubclient.check_url(hub)
    except (ValueError, tunnel.TunnelError) as error:
        raise click.UsageError(f"--hub: {error}") from None


def _status_lines(state: dict) -> list[str]:
    """What `live serve --status` prints for a bridge state."""
    lines = [f"{'running' if state['running'] else 'not running'}: node {state.get('node', '?')} via {state.get('hub', '?')}"]
    if state.get("last_error"):
        lines.append(f"  last error: {state['last_error']}")
    lines.append(f"  queued for the relay: {state['outbox']}")
    lines.append(f"  starts at login: {'yes' if service.installed() else 'no (one-conv live serve --install --yes)'}")
    accepts = bridge.settings().get(provision.ACCEPT_KEY) is True
    lines.append(f"  accepts teleports: {'yes' if accepts else 'no (one-conv live serve --accept-teleport)'}")
    lines.extend(f"  {note}" for note in state.get("notes") or [])
    return lines


def _wait_until_running(seconds: float) -> dict:
    deadline = time.monotonic() + seconds
    state = bridge.status()
    while not state["running"] and time.monotonic() < deadline:
        time.sleep(0.5)
        state = bridge.status()
    return state


def add_bridge_commands(live: click.Group, launcher: list[str]) -> None:
    @live.command("serve", context_settings=HELP,
                  epilog="\b\nExamples:\n"
                         "  one-conv live join https://<your-relay>.workers.dev#laptop.<code> --yes   # simplest: an invite\n"
                         "  one-conv live serve --hub https://<your-relay>.workers.dev --node laptop --token-stdin < token\n"
                         "  one-conv live serve --hub ssh://me@relay-box --node laptop --token-stdin < token\n"
                         "  one-conv live serve            # again, with the last settings\n"
                         "  one-conv live serve --status\n"
                         "  one-conv live serve --stop\n"
                         "  one-conv live serve --install --yes   # start at login, restart after a crash")
    @click.option("--hub", help="The relay: https://HOST (the Cloudflare Worker or a self-hosted relay behind TLS),"
                                " http://127.0.0.1:PORT (loopback only), or ssh://[user@]HOST to tunnel to it.")
    @click.option("--node", help="This machine's name on the relay, as its token was issued (a-z, 0-9, -).")
    @click.option("--hub-port", type=int, help="The relay's port on an ssh host.  [default: 8789]")
    @click.option("--token-stdin", is_flag=True, help="Read this node's token from stdin and remember it (mode 600).")
    @click.option("--status", "show_status", is_flag=True, help="Show whether the bridge runs, and its last state.")
    @click.option("--stop", "stop_it", is_flag=True, help="Stop the running bridge.")
    @click.option("--install", "install_it", is_flag=True,
                  help="Run the bridge as a login service (launchd on macOS, systemd on Linux): it starts on its own"
                       " and restarts after a crash.")
    @click.option("--uninstall", "uninstall_it", is_flag=True, help="Remove the login service.")
    @click.option("--accept-teleport/--no-accept-teleport", "accept_teleport", default=None,
                  help="Let agent sessions on other nodes of this relay teleport here (`live teleport`): they are"
                       " checked out and started in tmux on this machine. Saved; a running bridge applies it at once.")
    @click.option("--yes", is_flag=True, help="Apply --install or --uninstall; without it, only show what would change.")
    @click.option("--json", "as_json", is_flag=True, help="Emit --status as JSON.")
    def serve_command(hub, node, hub_port, token_stdin, show_status, stop_it, install_it, uninstall_it, accept_teleport,
                      yes, as_json):
        """Connect this machine to a live relay, so its agents and agents elsewhere reach each other.

        The bridge publishes the sessions running here, delivers messages the relay holds for
        them through their usual adapters, posts what `live send` queued while the relay was
        unreachable, and syncs this machine's agent histories to the relay's archive so any
        machine can read and continue them as NODE/harness:id. No inbound network access is
        required. Run one per machine, in the foreground, under tmux, or as a login service
        (--install). Settings, including the token, are saved in <live dir>/bridge/serve.json.

        The simplest way to connect a machine is `live join` with an invite from a machine
        already connected (`live invite NODE`). A token can also come from the relay's
        operator: `relay-worker/scripts/relay_admin.py token add NODE` for the Cloudflare
        Worker, `scripts/live-hub.py token add NODE` for the self-hosted Python relay.
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
            for line in _status_lines(state):
                click.echo(line)
            return
        if stop_it:
            click.echo("stopping the bridge" if bridge.stop() else "no bridge is running")
            return
        if accept_teleport is not None and (bridge.status()["running"] or not (hub or bridge.settings().get("hub"))):
            # The running bridge reads the setting for each request, so it needs no restart.
            bridge.save_accept_teleport(accept_teleport)
            click.echo(f"teleports to this machine are {'accepted' if accept_teleport else 'refused'}"
                       + (" (the running bridge applies it to the next request)" if bridge.status()["running"] else ""))
            return
        saved = bridge.settings()
        hub, node = hub or saved.get("hub"), node or saved.get("node")
        hub_port = hub_port or saved.get("hub_port") or 8789
        token = sys.stdin.read().strip() if token_stdin else os.environ.get("ONE_CONV_HUB_TOKEN") or saved.get("token")
        if not (hub and node and token):
            raise click.UsageError("Give --hub, --node and a token (--token-stdin or ONE_CONV_HUB_TOKEN) the first time,"
                                   " or run `one-conv live join` with an invite from a connected machine.")
        if hub != saved.get("hub") and not (token_stdin or os.environ.get("ONE_CONV_HUB_TOKEN")):
            # A remembered token belongs to the remembered hub; never send it to another one.
            raise click.UsageError("A new --hub needs its own token: pass --token-stdin or ONE_CONV_HUB_TOKEN.")
        hub = _check_hub(hub)
        bridge.save_settings(hub, node, hub_port, token,
                             **({provision.ACCEPT_KEY: accept_teleport} if accept_teleport is not None else {}))
        # force: the CLI configured WARNING at import, which would hide the bridge's transport and delivery lines.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", force=True)
        click.echo(f"bridge for node {node} via {hub}; stop with Ctrl-C or `one-conv live serve --stop`", err=True)
        sys.exit(bridge.serve(hub, node, token, hub_port))

    @live.command("invite", context_settings=HELP,
                  epilog="\b\nExamples:\n"
                         "  one-conv live invite studio\n"
                         "  one-conv live invite ci-runner --ttl 2h\n"
                         "  one-conv live invite laptop --replace     # the old laptop token stops working once it is used\n"
                         "  one-conv live invite studio --json")
    @click.argument("node")
    @click.option("--ttl", default=invite.DEFAULT_TTL, show_default=True,
                  help="How long the invite stays valid: 90s, 15m, 2h, 1d (at most 24h).")
    @click.option("--replace", is_flag=True,
                  help="Allow NODE to exist already: joining gives it a new token and the old one stops working.")
    @click.option("--json", "as_json", is_flag=True, help="Emit {node, expires, command} as JSON.")
    def invite_command(node, ttl, replace, as_json):
        """Let a new machine join the relay as NODE: prints the one command to run there.

        The relay makes a one-time code bound to NODE with this machine's node token, so no
        admin secret is copied around. The code expires after --ttl and works once; anyone
        holding it until then can join as NODE, so pass it like a password.
        """
        saved = bridge.settings()
        if not (saved.get("hub") and saved.get("token")):
            raise click.ClickException("This machine is not connected to a relay: run `one-conv live join` with an"
                                       " invite from a connected machine, or `one-conv live serve --hub ... --token-stdin`.")
        try:
            seconds = invite.parse_ttl(ttl)
            with invite.reach(saved["hub"], saved.get("hub_port") or invite.DEFAULT_HUB_PORT) as url:
                made = invite.create(url, saved["token"], node, seconds, replace)
        except (invite.InviteError, tunnel.TunnelError, ValueError) as error:
            raise click.ClickException(str(error)) from None
        command = invite.join_command(saved["hub"], made["node"], made["code"],
                                      saved.get("hub_port") or invite.DEFAULT_HUB_PORT)
        if as_json:
            click.echo(json.dumps({"node": made["node"], "expires": made["expires"], "command": command}))
            return
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(made["expires"]))
        click.echo(f"Run this on the new machine before {until} (works once):", err=True)
        click.echo(command)

    @live.command("join", context_settings=HELP,
                  epilog="\b\nExamples:\n"
                         "  one-conv live join 'https://<your-relay>.workers.dev#studio.<code>'          # show the steps\n"
                         "  one-conv live join 'https://<your-relay>.workers.dev#studio.<code>' --yes")
    @click.argument("invitation", metavar="INVITE")
    @click.option("--hub-port", type=int, default=invite.DEFAULT_HUB_PORT, show_default=True,
                  help="The relay's port on an ssh host.")
    @click.option("--accept-teleport", is_flag=True,
                  help="Also let agent sessions on other nodes teleport here (`live teleport`).")
    @click.option("--yes", is_flag=True, help="Join. Without it, only lists the steps.")
    def join_command(invitation, hub_port, accept_teleport, yes):
        """Connect this machine to the relay with an invite from `live invite`, and start its bridge.

        Redeems the one-time code for this node's token, saves the relay, node and token like
        `live serve --token-stdin` (mode 600), applies the `live install` steps, installs the
        bridge as a login service (`live serve --install`) and starts it.
        """
        try:
            hub, node, code = invite.parse_invite(invitation)
        except invite.InviteError as error:
            raise click.UsageError(str(error)) from None
        hub = _check_hub(hub)
        saved = bridge.settings()
        missing = [step for step in installer.plan(launcher) if step.available() and not step.done()]
        if not yes:
            click.echo("DRY-RUN (nothing changed). Re-run with --yes to join.")
            click.echo(f"  redeem   the one-time invite for node {node} at {hub}")
            if saved.get("hub"):
                click.echo(f"  replace  the saved bridge settings (node {saved.get('node')} via {saved['hub']})")
            click.echo(f"  save     relay, node and token in {bridge.bridge_dir() / 'serve.json'} (mode 600)")
            if accept_teleport:
                click.echo("  accept   sessions teleported here from other nodes (live teleport)")
            for step in missing:
                click.echo(f"  install  [{step.harness}] {step.summary}")
            click.echo(f"  install  the login service {service.path()} and start the bridge")
            return
        try:
            # Checked first: the code is spent once redeemed.
            service.require_supported()
            with invite.reach(hub, hub_port) as url:
                joined = invite.redeem(url, code)
        except (invite.InviteError, tunnel.TunnelError, RuntimeError, ValueError) as error:
            raise click.ClickException(str(error)) from None
        bridge.save_settings(hub, joined["node"], hub_port, joined["token"],
                             **({provision.ACCEPT_KEY: True} if accept_teleport else {}))
        click.echo(f"  saved    node {joined['node']} via {hub} in {bridge.bridge_dir() / 'serve.json'}")
        for step in missing:
            step.apply()
            click.echo(f"  applied  [{step.harness}] {step.summary}")
        try:
            notes = service.install()
        except RuntimeError as error:
            raise click.ClickException(f"{error}; the token is saved, so retry with "
                                       "`one-conv live serve --install --yes`") from None
        for note in notes:
            click.echo(f"  {note}")
        # The bridge reports its node and relay once it runs; until then they come from the settings.
        for line in _status_lines({"node": joined["node"], "hub": hub, **_wait_until_running(JOIN_WAIT_SECONDS)}):
            click.echo(line)
