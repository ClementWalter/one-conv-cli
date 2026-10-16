"""`one-conv live spawn | spawned | stop`: bring more agents into the work without opening terminals."""

import json
import re
import sys
import uuid
from pathlib import Path

import click

from . import router, spawn
from .cli import examples

HELP = {"help_option_names": ["-h", "--help"]}
HUB_REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def introduction(prompt: str, sender) -> str:
    """The first prompt, preceded by who started this agent and how to answer them."""
    who = f"{sender.address}" + (f" ({sender.title})" if sender and sender.title else "") if sender else "the user"
    return (f"You were started by {who} with `one-conv live spawn`, as a teammate on the same task. "
            f"Answer teammates with the one-conv send_message tool or `one-conv live send ADDRESS 'text' --yes`.\n\n"
            f"{prompt}")


def add_spawn_commands(live: click.Group) -> None:
    @live.command("spawn", context_settings=HELP,
                  epilog="\b\nExamples:\n  one-conv live spawn codex 'review the diff on this branch' --yes\n"
                         "  one-conv live spawn claude 'write the tests' --model sonnet --cwd ../repo --yes\n"
                         "  one-conv live spawn omp --stdin --yes < brief.md\n"
                         "  one-conv live spawn codex 'review the release' --cloud --name jack --topic release --yes")
    @click.argument("harness", type=click.Choice(spawn.HARNESSES))
    @click.argument("prompt", required=False)
    @click.option("--stdin", "from_stdin", is_flag=True, help="Read the first prompt from stdin.")
    @click.option("--model", help="Model for the new agent, in that harness's own naming.")
    @click.option("--cloud", is_flag=True, help="Provision on the configured cloud workspace, reusing a shared worker box.")
    @click.option("--name", help="Stable teammate name; required for --cloud.")
    @click.option("--topic", help="Topic to join; required for --cloud.")
    @click.option("--profile", default="default", show_default=True, help="Cloud repository/tool/credential profile.")
    @click.option("--request-id", help="Reuse the printed 32-hex ID after an unconfirmed cloud request.")
    @click.option("--cwd", type=click.Path(file_okay=False, path_type=Path), default=".",
                  help="Working directory for the new agent.  [default: current directory]")
    @click.option("--arg", "extra", multiple=True,
                  help="Extra argument for the harness command, repeatable (e.g. --arg=--dangerously-skip-permissions).")
    @click.option("--no-intro", is_flag=True, help="Send the prompt as is, without saying who started the agent.")
    @click.option("--trust-folder", is_flag=True,
                  help="Answer yes to Claude Code's first-visit question about trusting --cwd.")
    @click.option("--wait", "wait_seconds", default=60.0, show_default=True,
                  help="Seconds to wait for the agent to register as a live session.")
    @click.option("--yes", is_flag=True, help="Actually start it. Without it, prints what would run.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the spawned agent as JSON.")
    def spawn_command(harness, prompt, from_stdin, model, cloud, name, topic, profile, request_id, cwd, extra,
                      no_intro, trust_folder, wait_seconds, yes, as_json):
        """Start a resident Claude Code, Codex or Oh My Pi teammate, locally or in the cloud.

        With --cloud, the configured workspace prepares repositories, tools and provider
        access in a shared worker box. --name and --topic are required. The receipt gives
        a durable request ID; inspect its progress with live workers.

        The agent runs interactively, so it stays reachable with `live send` after its first
        turn, unlike `codex exec` or `claude -p`, which exit. Watch it with `tmux attach -t NAME`;
        end it with `live stop`. It inherits your usual permission settings for that harness,
        so an unattended agent may wait on a prompt: pass the harness's own flags with --arg.
        """
        if from_stdin:
            prompt = sys.stdin.read()
        if not prompt:
            raise click.UsageError("Give PROMPT or --stdin.")
        if cloud:
            from ..control import client
            from . import hubclient
            if not name or not topic:
                raise click.UsageError("--cloud requires --name and --topic")
            if extra or trust_folder or no_intro or cwd != Path("."):
                raise click.UsageError("Cloud permissions, tools and repository are chosen by --profile; omit local-only flags")
            ident = request_id or uuid.uuid4().hex
            if not HUB_REQUEST_ID.fullmatch(ident):
                raise click.UsageError("--request-id must be 32 lowercase hexadecimal characters")
            spec = {"name": name, "harness": harness, "model": model, "topic": topic.removeprefix("#"),
                    "profile": profile, "prompt": prompt}
            if not yes:
                click.echo(json.dumps({"dry_run": True, "id": ident, "spec": spec}))
                return
            try:
                result = client.submit(spec, ident)
            except (ValueError, hubclient.HubRefused, hubclient.HubUnavailable) as error:
                raise click.ClickException(str(error)) from None
            click.echo(json.dumps(result) if as_json else f"{result['status']}: {name} (request {ident}); inspect with one-conv live workers")
            return
        if name or topic or request_id:
            raise click.UsageError("--name, --topic and --request-id currently require --cloud")
        if not cwd.is_dir():
            raise click.UsageError(f"--cwd {cwd} is not a directory.")
        text = prompt if no_intro else introduction(prompt, router.whoami())
        try:
            argv = spawn.command(harness, text, model, tuple(extra))
            if not yes:
                click.echo("DRY-RUN (nothing started). Re-run with --yes to start.")
                click.echo(f"  in tmux: {' '.join(argv[:-1])} <prompt, {len(text)} chars>")
                click.echo(f"  cwd:     {cwd.resolve()}")
                return
            started = spawn.start(harness, text, cwd.resolve(), model, tuple(extra), wait_seconds, trust_folder)
        except spawn.SpawnError as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps(started.to_json()))
            return
        click.echo(f"started: {started.address}  (tmux session {started.tmux})")
        click.echo(f"  message it: one-conv live send {started.address} 'text' --yes")
        click.echo(f"  watch it:   tmux attach -t {started.tmux}")

    @live.command("workers", context_settings=HELP, epilog=examples(
        "one-conv live workers --json", "one-conv live workers --stop REQUEST_ID --yes"))
    @click.option("--stop", "stop_id", help="Stop one tracked cloud teammate, leaving the shared box running.")
    @click.option("--recheck", help="Recheck provider login/readiness on the same worker box.")
    @click.option("--yes", is_flag=True, help="Apply a stop or recheck.")
    @click.option("--json", "as_json", is_flag=True)
    def workers_command(stop_id, recheck, yes, as_json):
        """Inspect managed cloud teammates and their shared worker boxes."""
        from ..control import client
        from . import hubclient
        if stop_id and recheck:
            raise click.UsageError("Choose --stop or --recheck")
        if (stop_id or recheck) and not yes:
            raise click.UsageError("Stopping or rechecking requires --yes")
        try:
            result = client.workers("POST", {"action": "stop" if stop_id else "recheck", "id": stop_id or recheck}) if stop_id or recheck else client.workers()
        except (ValueError, hubclient.HubUnavailable, hubclient.HubRefused) as error:
            raise click.ClickException(str(error)) from None
        if as_json or stop_id or recheck:
            click.echo(json.dumps(result))
            return
        for job in result.get("jobs", []):
            click.echo(f"{job['id']}  {job['spec']['name']}  {job['status']}  {job.get('address') or ''}")

    @live.command("spawned", context_settings=HELP, epilog=examples('one-conv live spawned', 'one-conv live spawned --json'))
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def spawned_command(as_json):
        """List agents started with `live spawn` whose tmux session is still running."""
        rows = spawn.listing()
        if as_json:
            click.echo(json.dumps(rows))
            return
        if not rows:
            click.echo("No spawned agents are running.", err=True)
        for row in rows:
            click.echo(f"{row['address'] or '(not registered)'}  {row['harness']:<7}  {row['tmux']}  {row['cwd']}")

    @live.command("stop", context_settings=HELP, epilog=examples('one-conv live stop codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03   # the full address `live spawned` shows',
                                                                 'one-conv live stop oneconv-codex-1a2b3c4d'))
    @click.argument("target")
    def stop_command(target):
        """End a spawned agent, by its address or tmux session name."""
        try:
            row = spawn.stop(target)
        except spawn.SpawnError as error:
            raise click.ClickException(str(error)) from None
        click.echo(f"stopped: {row['address'] or row['tmux']}")
