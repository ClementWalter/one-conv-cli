"""`one-conv live spawn | spawned | stop`: bring more agents into the work without opening terminals."""

import json
import sys
from pathlib import Path

import click

from . import router, spawn

HELP = {"help_option_names": ["-h", "--help"]}


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
                         "  one-conv live spawn omp --stdin --yes < brief.md")
    @click.argument("harness", type=click.Choice(spawn.HARNESSES))
    @click.argument("prompt", required=False)
    @click.option("--stdin", "from_stdin", is_flag=True, help="Read the first prompt from stdin.")
    @click.option("--model", help="Model for the new agent, in that harness's own naming.")
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
    def spawn_command(harness, prompt, from_stdin, model, cwd, extra, no_intro, trust_folder, wait_seconds, yes,
                      as_json):
        """Start a Claude Code, Codex or Oh My Pi agent detached in tmux and print its address.

        The agent runs interactively, so it stays reachable with `live send` after its first
        turn, unlike `codex exec` or `claude -p`, which exit. Watch it with `tmux attach -t NAME`;
        end it with `live stop`. It inherits your usual permission settings for that harness,
        so an unattended agent may wait on a prompt: pass the harness's own flags with --arg.
        """
        if from_stdin:
            prompt = sys.stdin.read()
        if not prompt:
            raise click.UsageError("Give PROMPT or --stdin.")
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

    @live.command("spawned", context_settings=HELP)
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

    @live.command("stop", context_settings=HELP)
    @click.argument("target")
    def stop_command(target):
        """End a spawned agent, by its address or tmux session name."""
        try:
            row = spawn.stop(target)
        except spawn.SpawnError as error:
            raise click.ClickException(str(error)) from None
        click.echo(f"stopped: {row['address'] or row['tmux']}")
