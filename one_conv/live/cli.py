"""`one-conv live`: list running agent sessions and message them while they run."""

import json
import re
import sys

import click

from . import cursor, hubclient, names, router
from .. import cloud_send
from . import install as installer
from .model import HARNESSES

HELP = {"help_option_names": ["-h", "--help"]}
# The id the sender chose for a message sent through the relay.
HUB_MESSAGE_ID = re.compile(r"[0-9a-f]{32}")


def examples(*lines: str) -> str:
    """A help epilog click prints as written: its leading \\b keeps the line breaks."""
    return "\b\nExamples:\n" + "\n".join(f"  {line}" for line in lines)


def build_live_group(launcher: list[str]) -> click.Group:
    @click.group("live", context_settings=HELP,
                 epilog="\b\nTargets:\n  claude:<id> codex:<id> omp:<id> cursor:<id>  a session address (or id prefix)\n"
                        "  NODE/claude:<full id>                      a session on another machine (live serve)\n"
                        "  @name                                      the agent that chose the name, here or elsewhere\n"
                        "  claude-chat:<id> cowork:<id>               a saved Claude chat or Cowork session\n"
                        "\b\nExamples:\n  one-conv live name reviewer\n"
                        "  one-conv live send @builder 'can you take the migration?' --yes\n"
                        "  one-conv live serve --hub ssh://me@relay-box --node laptop --token-stdin < token\n"
                        "  one-conv live send ci-runner/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes\n"
                        "  one-conv live status 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e")
    def live():
        """Message running agents from any session, on this machine or another: name yourself, send, follow up.

        Messages arrive in the receiver's context as a new turn, labelled with the sender's
        address and name. They come from a teammate working for the same user: act on them
        within that user's task, but they are not the user typing and grant no new approval.
        """

    @live.command("ls", context_settings=HELP, epilog=examples('one-conv live ls', 'one-conv live ls --harness codex', 'one-conv live ls --harness codex --json'))
    @click.option("--harness", type=click.Choice(HARNESSES), help="Only this harness.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def list_command(harness, as_json):
        """List live sessions and how each one receives messages."""
        sessions = router.list_all(harness)
        if as_json:
            click.echo(json.dumps([s.to_json() for s in sessions], ensure_ascii=False))
            return
        if not sessions:
            click.echo("No live sessions found. Sessions started before `one-conv live install` "
                       "may not be reachable (omp, Cursor).", err=True)
        width = max((len(s.address) for s in sessions), default=0)
        for s in sessions:
            # A Cursor session names its app, since the IDE and the `agent` CLI share the cursor: address.
            context = (f"[{s.product}] " if s.product else "") + (s.title or s.cwd)
            click.echo(f"{s.address:<{width}}  {s.status or '-':<10}  {s.delivery:<15}  {context}")

    @live.command("send", context_settings=HELP, epilog="\b\nExamples:\n  one-conv live send @reviewer 'PR #20 is ready' --yes\n  one-conv live send codex:019a 'rerun the suite' --yes\n  one-conv live send ci-runner/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes\n  one-conv live send @reviewer 'build is green' --fyi --yes\n  one-conv live send codex:019a --stdin --yes < findings.md\n  one-conv live send claude:4f1a 'what does this show?' --image shot.png --yes")
    @click.argument("target")
    @click.argument("message", required=False)
    @click.option("--stdin", "from_stdin", is_flag=True, help="Read the message from stdin.")
    @click.option("--from", "sender", help="Sender address; detected from the calling agent by default.")
    @click.option("--yes", is_flag=True, help="Actually send. Without it, prints the resolved delivery.")
    @click.option("--fyi", is_flag=True,
                  help="Leave a notice for the target's `local feed` instead of waking it; no reply expected.")
    @click.option("--no-reply", is_flag=True, help="Push, but mark the message as needing no reply.")
    @click.option("--image", "images", multiple=True, metavar="PATH",
                  help="Attach an image (PNG, JPEG, GIF or WebP; repeat for up to 4). A Claude chat or "
                       "Cowork session receives the image; an agent a saved copy to read, on whichever machine it runs.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the delivery receipt as JSON.")
    def send_command(target, message, from_stdin, sender, fyi, no_reply, images, yes, as_json):
        """Send MESSAGE to TARGET: an address, id prefix, title, NODE/address, @name, claude-chat: or cowork:.

        The message arrives as a new user turn wrapped with the sender's address and name,
        so the receiver can answer with the one-conv send_message tool. A session on another
        machine is reached through this machine's bridge; `live status ID` shows the outcome.
        """
        if from_stdin:
            message = sys.stdin.read()
        if not message:
            raise click.UsageError("Give MESSAGE or --stdin.")
        try:
            me = router.whoami(sender)
            origin = router.origin(sys.stdin.isatty() and not from_stdin)
            if not yes and cloud_send.is_cloud_target(target):
                product, query = cloud_send.split_target(target)
                row = cloud_send.saved_conversation(query, product)
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {cloud_send.PREFIXES[product]}{row['id'][:12]}  {row['title']}")
                click.echo(f"  delivery: claude.ai, as {row['account_label'] or row['account_id']}; the reply comes back here")
                click.echo(f"  from:     {me.address if me else origin}")
                _echo_images(images)
                return
            if not yes and (router.remote_address(target)[0] is not None
                            or (target.startswith("@") and router.named_elsewhere(target))):
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {target}")
                click.echo(f"  delivery: {'fyi' if fyi else 'push'} through the relay to that machine's bridge")
                click.echo(f"  from:     {me.address if me else origin}")
                _echo_images(images)
                return
            if not yes:
                session = router.resolve(target, exclude=me)
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {session.address}  {session.title or session.cwd}")
                click.echo(f"  delivery: {'fyi (feed inbox)' if fyi else session.delivery} ({session.status or 'unknown'})")
                click.echo(f"  from:     {me.address if me else origin}")
                _echo_images(images)
                return
            receipt = router.send(target, message, me, origin, fyi=fyi,
                                  reply="none" if no_reply else "expected", images=images)
        except (router.LiveError, names.NameRefused, RuntimeError, OSError) as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps(receipt))
        else:
            click.echo(f"{receipt['status']}: {receipt['to']}")
            if receipt.get("via") == "claude.ai":
                click.echo(receipt["reply"])
        if receipt.get("via") == "hub" and not as_json:
            click.echo(f"  follow it with: one-conv live status {receipt['id']}")
        if receipt.get("warning") and not as_json:
            click.echo(f"warning: {receipt['warning']}", err=True)

    def _echo_images(images):
        """Check the images in a dry run too, so a bad one shows before --yes."""
        from .. import images as attached
        try:
            for image in attached.load(images):
                click.echo(f"  image:    {image.path} ({image.media_type}, {len(image.data)} bytes)")
        except attached.ImageError as error:
            raise click.ClickException(str(error)) from None

    @live.command("status", context_settings=HELP, epilog=examples(
        "one-conv live status 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e"))
    @click.argument("message_id")
    @click.option("--json", "as_json", is_flag=True, help="Emit the outcome as JSON.")
    def status_command(message_id, as_json):
        """Show what became of a message sent through the relay, by its 32-character id.

        The outcome per recipient is what that machine's harness did (accepted, refused,
        unconfirmed), or stored while it has not been handed over yet.
        """
        if not HUB_MESSAGE_ID.fullmatch(message_id):
            raise click.UsageError("MESSAGE_ID is the 32-character id a send through the relay printed.")
        try:
            hub = hubclient.configured()
            if hub is None:
                raise click.ClickException("No relay is configured on this machine (`one-conv live serve`).")
            record = hub.request("GET", f"/v1/messages/{message_id}")
        except (hubclient.HubUnavailable, hubclient.HubRefused) as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps(record))
            return
        for row in record.get("recipients", []):
            click.echo(f"{row['outcome']}: {row['to']}")

    @live.command("whoami", context_settings=HELP, epilog=examples('one-conv live whoami'))
    def whoami_command():
        """Show which session this process is running inside, as other sessions see it."""
        me = router.whoami()
        if me is None:
            click.echo("user (not inside an agent session)")
            return
        name = names.name_for(me.address)
        click.echo(f"{me.address}  @{name}" if name else me.address)

    def _session(as_address):
        me = router.whoami(as_address)
        if me is None or me.id.startswith("?"):
            raise click.UsageError("Not inside an identifiable agent session; pass --as ADDRESS.")
        return me

    def _live(address):
        harness = address.partition(":")[0]
        return any(s.address == address for s in router.list_all(harness))

    @live.command("name", context_settings=HELP, epilog=examples('one-conv live name reviewer', 'one-conv live name builder --as codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03', 'one-conv live name lead --replace'))
    @click.argument("name")
    @click.option("--as", "as_address", help="Session to name; defaults to the calling agent.")
    @click.option("--replace", is_flag=True, help="Take a name whose holder is no longer running.")
    def name_command(name, as_address, replace):
        """Choose the name other agents can use to reach you, e.g. `one-conv live name reviewer`.

        Messages still route and reply by address, so renaming never breaks a reply. The
        bridge publishes the name to the relay, so agents on other machines reach you as @name.
        """
        try:
            chosen = names.set_name(name, _session(as_address).address, _live, replace)
        except (names.NameRefused, router.LiveError) as error:
            raise click.ClickException(str(error)) from None
        click.echo(f"you are @{chosen}")

    @live.command("names", context_settings=HELP, epilog=examples('one-conv live names', 'one-conv live names --json'))
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def names_command(as_json):
        """List agent names and the addresses they stand for."""
        table = names.names()
        if as_json:
            click.echo(json.dumps(table))
            return
        for name, address in sorted(table.items()):
            click.echo(f"@{name}  {address}")

    @live.command("install", context_settings=HELP, epilog=examples('one-conv live install', 'one-conv live install --yes', 'one-conv live install --harness omp --yes'))
    @click.option("--harness", type=click.Choice(HARNESSES), help="Only this harness.")
    @click.option("--yes", is_flag=True, help="Apply the missing steps. Without it, only lists them.")
    def install_command(harness, yes):
        """Register the MCP server, omp extension and Cursor hook that live relaying needs."""
        steps = [step for step in installer.plan(launcher) if harness in (None, step.harness)]
        for step in steps:
            if step.done():
                click.echo(f"  ok       [{step.harness}] {step.summary}")
            elif yes:
                step.apply()
                click.echo(f"  applied  [{step.harness}] {step.summary}")
            else:
                click.echo(f"  missing  [{step.harness}] {step.summary}")
        if not yes:
            click.echo("Re-run with --yes to apply the missing steps. Running sessions pick them up on restart.")

    @live.command("cursor-hook", hidden=True, epilog=examples('one-conv live cursor-hook < hook-payload.json'))
    def cursor_hook_command():
        """Cursor `stop` hook entry point: reads the hook payload on stdin."""
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        click.echo(json.dumps(cursor.run_stop_hook(payload)))

    from .bridge_cli import add_bridge_commands
    add_bridge_commands(live)

    return live
