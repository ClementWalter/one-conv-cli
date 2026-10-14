"""`one-conv live`: list running agent sessions and message them while they run."""

import json
import sys

import click

from . import cursor, router
from . import install as installer
from .model import HARNESSES

HELP = {"help_option_names": ["-h", "--help"]}


def build_live_group(launcher: list[str]) -> click.Group:
    @click.group("live", context_settings=HELP,
                 epilog="\b\nExamples:\n  one-conv live install --yes\n  one-conv live ls\n"
                        "  one-conv live send codex:019a... 'run the tests' --yes")
    def live():
        """Message running Claude Code, Codex, Oh My Pi and Cursor sessions."""

    @live.command("ls", context_settings=HELP)
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
            click.echo(f"{s.address:<{width}}  {s.status or '-':<10}  {s.delivery:<15}  {s.title or s.cwd}")

    @live.command("send", context_settings=HELP)
    @click.argument("target")
    @click.argument("message", required=False)
    @click.option("--stdin", "from_stdin", is_flag=True, help="Read the message from stdin.")
    @click.option("--from", "sender", help="Sender address; detected from the calling agent by default.")
    @click.option("--yes", is_flag=True, help="Actually send. Without it, prints the resolved delivery.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the delivery receipt as JSON.")
    def send_command(target, message, from_stdin, sender, yes, as_json):
        """Send MESSAGE into the running session TARGET (an address, id prefix, title or cwd).

        The message arrives as a new user turn wrapped with the sender's address, so the
        receiver can answer with the one-conv send_message tool.
        """
        if from_stdin:
            message = sys.stdin.read()
        if not message:
            raise click.UsageError("Give MESSAGE or --stdin.")
        try:
            me = router.whoami(sender)
            origin = "user" if sys.stdin.isatty() and not from_stdin else "unknown"
            if not yes:
                session = router.resolve(target, exclude=me)
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {session.address}  {session.title or session.cwd}")
                click.echo(f"  delivery: {session.delivery} ({session.status or 'unknown'})")
                click.echo(f"  from:     {me.address if me else origin}")
                return
            receipt = router.send(target, message, me, origin)
        except (router.LiveError, RuntimeError, OSError) as error:
            raise click.ClickException(str(error)) from None
        click.echo(json.dumps(receipt) if as_json else f"{receipt['status']}: {receipt['to']}")

    @live.command("whoami", context_settings=HELP)
    def whoami_command():
        """Show which session this process is running inside, as other sessions see it."""
        me = router.whoami()
        click.echo(me.address if me else "user (not inside an agent session)")

    @live.command("install", context_settings=HELP)
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

    @live.command("cursor-hook", hidden=True)
    def cursor_hook_command():
        """Cursor `stop` hook entry point: reads the hook payload on stdin."""
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        click.echo(json.dumps(cursor.run_stop_hook(payload)))

    return live
