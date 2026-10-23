"""`one-conv live routine`: prompts the workspace posts in a channel on a schedule, as the human would."""

import json
import os
import sys
import uuid

import click

from .cli import examples

HELP = {"help_option_names": ["-h", "--help"]}


def local_zone() -> str:
    """This machine's IANA zone: $TZ, else where /etc/localtime points, else UTC."""
    if os.environ.get("TZ", "").count("/") == 1:
        return os.environ["TZ"]
    target = os.path.realpath("/etc/localtime")
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"


def call(method, payload=None):
    from ..control import client
    from . import hubclient
    try:
        return client.routines(method, payload)
    except (ValueError, hubclient.HubUnavailable, hubclient.HubRefused) as error:
        raise click.ClickException(str(error)) from None


def find(query: str) -> dict:
    """A routine by id, id prefix or exact name."""
    rows = call("GET")["routines"]
    matches = [r for r in rows if r["id"] == query or r["name"] == query] or [r for r in rows if r["id"].startswith(query)]
    if len(matches) != 1:
        raise click.ClickException(f"{len(matches)} routines match {query!r}; give its id from `live routine ls`.")
    return matches[0]


def add_routine_commands(live: click.Group) -> None:
    @live.group("routine", context_settings=HELP, epilog=examples(
        "one-conv live routine ls",
        "one-conv live routine add --name daily-review --topic reviews --cron '0 21 * * *' "
        "'Follow heartbeats/daily-review/daily-review.md' --yes",
        "one-conv live routine run daily-review --yes"))
    def routine():
        """Scheduled prompts: at each time the workspace posts the prompt in its channel as you.

        Agents working in the channel are woken, and a channel nobody works in gets a session
        started (this machine's spawner first, then a cloud worker). Changing routines needs the
        staffing grant (`live control node grant-staffing NODE`), since a routine posts as you.
        """

    @routine.command("ls", context_settings=HELP, epilog=examples("one-conv live routine ls", "one-conv live routine ls --json"))
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def list_command(as_json):
        """List routines with their schedule, channel and next run."""
        rows = call("GET")["routines"]
        if as_json:
            click.echo(json.dumps(rows, ensure_ascii=False))
            return
        if not rows:
            click.echo("No routines yet. Add one with `one-conv live routine add`.", err=True)
        for r in rows:
            state = "on " if r["enabled"] else "off"
            click.echo(f"{r['id'][:8]}  {state}  #{r['topic']:<16}  {r['schedule']} ({r['tz']})  {r['name']}")

    @routine.command("add", context_settings=HELP, epilog=examples(
        "one-conv live routine add --name standup --topic team --cron '30 9 * * 1-5' 'Post the standup summary' --yes",
        "one-conv live routine add --name weekly --topic ops --cron '0 8 * * 1' --stdin --yes < weekly.md"))
    @click.argument("prompt", required=False)
    @click.option("--name", required=True, help="What the routine is called in the app.")
    @click.option("--topic", required=True, help="The channel it posts in, without #.")
    @click.option("--cron", "cron", required=True, help="Five-field cron: minute hour day month weekday.")
    @click.option("--tz", default=None, help="IANA time zone for the schedule.  [default: this machine's]")
    @click.option("--stdin", "from_stdin", is_flag=True, help="Read the prompt from stdin.")
    @click.option("--paused", is_flag=True, help="Save it without scheduling it.")
    @click.option("--id", "ident", help="Replace the routine with this id instead of adding one.")
    @click.option("--yes", is_flag=True, help="Save it. Without it, prints what would be saved.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the saved routine as JSON.")
    def add_command(prompt, name, topic, cron, tz, from_stdin, paused, ident, yes, as_json):
        """Add a routine, or replace one with --id."""
        text = sys.stdin.read() if from_stdin else prompt
        if not text or not text.strip():
            raise click.UsageError("Give PROMPT or --stdin.")
        routine = {"id": ident or uuid.uuid4().hex, "name": name, "topic": topic.lstrip("#"), "prompt": text,
                   "cron": cron, "tz": tz or local_zone(), "enabled": not paused}
        if not yes:
            click.echo("DRY-RUN (nothing saved). Re-run with --yes to save.")
            click.echo(json.dumps({**routine, "prompt": f"<{len(text)} chars>"}, ensure_ascii=False))
            return
        saved = call("POST", {"action": "save", "routine": routine})
        click.echo(json.dumps(saved, ensure_ascii=False) if as_json else
                   f"saved {saved['id'][:8]}: {saved['schedule']} ({saved['tz']}) in #{saved['topic']}")

    def action(name, verb, help_text):
        @routine.command(name, help=help_text + ".", context_settings=HELP,
                         epilog=examples(f"one-conv live routine {name} ROUTINE --yes"))
        @click.argument("query", metavar="ROUTINE")
        @click.option("--yes", is_flag=True, help=f"{help_text.split(';')[0]}. Without it, names the routine only.")
        def command(query, yes):
            row = find(query)
            if not yes:
                click.echo(f"DRY-RUN: would {name} {row['id'][:8]} {row['name']!r}. Re-run with --yes.")
                return
            result = call("POST", {"id": row["id"], "action": verb})
            click.echo(json.dumps(result, ensure_ascii=False))
        return command

    action("run", "run", "Post the routine's prompt in its channel now; its schedule is unchanged")
    action("pause", "pause", "Stop the routine firing until it is resumed")
    action("resume", "resume", "Schedule the routine again from now")
    action("rm", "delete", "Delete the routine; posts it already made stay in the channel")

