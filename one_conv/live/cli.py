"""`one-conv live`: list running agent sessions and message them while they run."""

import json
import os
import re
import shlex
import sys
import time

import click

from . import agy, cursor, held, hubclient, names, router, share, spawn
from .. import cloud_send
from . import install as installer
from .model import HARNESSES

HELP = {"help_option_names": ["-h", "--help"]}
# The id the sender chose for a message sent through the relay.
HUB_MESSAGE_ID = re.compile(r"[0-9a-f]{32}")


def examples(*lines: str) -> str:
    """A help epilog click prints as written: its leading \\b keeps the line breaks."""
    return "\b\nExamples:\n" + "\n".join(f"  {line}" for line in lines)


def build_live_group(launcher: list[str], transcript=None) -> click.Group:
    """The `live` commands; `transcript(address)` reads a local session for `live teleport` (see one_conv_cli)."""
    @click.group("live", context_settings=HELP,
                 epilog="\b\nTargets:\n  claude:<id> codex:<id> omp:<id> cursor:<id> agy:<id>  a session address (or id prefix)\n"
                        "  NODE/claude:<full id>                      a session on another machine (live join, live serve)\n"
                        "  @name                                      the agent that chose the name, here or elsewhere\n"
                        "  claude-chat:<id> cowork:<id>               a saved Claude chat or Cowork session\n"
                        "  user                                       you: a note in your inbox (`live inbox`), never a new turn\n"
                        "  guest:<share id>                           the guest of a share link (`live share`), answered on its page\n"
                        "\b\nExamples:\n  one-conv live name reviewer\n"
                        "  one-conv live share --label Alice --yes   # inside a session: a link to chat with it alone\n"
                        "  one-conv live send @builder 'can you take the migration?' --yes\n"
                        "  one-conv live invite studio      # on a connected machine: prints the join command\n"
                        "  one-conv live join 'https://<your-relay>.workers.dev#studio.<code>' --yes   # on the new one\n"
                        "  one-conv live serve --hub ssh://me@relay-box --node laptop --token-stdin < token   # self-hosted relay\n"
                        "  one-conv live send ci-runner/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes\n"
                        "  one-conv live teleport studio --check     # from an agent: what studio lacks to continue it\n"
                        "  one-conv live serve --accept-teleport     # on studio: let sessions teleport here\n"
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
                       "may not be reachable (omp, Cursor, agy).", err=True)
        width = max((len(s.address) for s in sessions), default=0)
        for s in sessions:
            # A Cursor session names its app, since the IDE and the `agent` CLI share the cursor: address.
            context = (f"[{s.product}] " if s.product else "") + (s.title or s.cwd)
            click.echo(f"{s.address:<{width}}  {s.status or '-':<10}  {s.delivery:<15}  {context}")

    @live.command("send", context_settings=HELP, epilog="\b\nExamples:\n  one-conv live send @reviewer 'PR #20 is ready' --yes\n  one-conv live send codex:019a 'rerun the suite' --yes\n  one-conv live send ci-runner/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes\n  one-conv live send @reviewer 'build is green' --fyi --yes\n  one-conv live send @builder 'after this, rebase on main' --when idle --yes\n  one-conv live send codex:019a --stdin --yes < findings.md\n  one-conv live send claude:4f1a 'what does this show?' --image shot.png --yes\n  one-conv live send 'https://<relay>/s/<id>#<secret>' 'what changed in the API?' --yes   # a share link")
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
    @click.option("--tag", "tags", multiple=True, metavar="TAG",
                  help="Label a message to user (repeatable); `live inbox --tag` filters by it.")
    @click.option("--when", type=click.Choice(held.WHEN), default="now", show_default=True,
                  help="now: a busy agent takes it at its next tool boundary. idle: hold it until the "
                       "agent's current turn ends, so it does not steer the run (needs `live serve`).")
    @click.option("--no-wait", is_flag=True,
                  help="To a share link: post without waiting for the session's answer.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the delivery receipt as JSON.")
    def send_command(target, message, from_stdin, sender, fyi, no_reply, images, tags, when, no_wait, yes, as_json):
        """Send MESSAGE to TARGET: an address, id prefix, title, NODE/address, @name, claude-chat:, cowork: or share link.

        A share link (`https://<relay>/s/<id>#<secret>`, from someone's `live share`) reaches the
        one session they shared; its answer is printed, like a claude-chat: reply.

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
            if not yes and target == router.USER:
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       user, in {'the relay' if hubclient.configured() else 'this machine'}'s inbox"
                           f"{' tagged ' + ', '.join(router.check_tags(list(tags))) if tags else ''}")
                click.echo(f"  from:     {me.address if me else origin}")
                return
            if not yes and share.is_link(target):
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {share.redacted(target)}, a session shared with you")
                click.echo("  delivery: posted to the share; " + ("no wait" if no_wait else "its answer is printed here"))
                click.echo(f"  from:     {me.address if me else origin}")
                return
            if not yes and target.startswith(router.GUEST):
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {target}, the guest of a share (`live share --list`)")
                click.echo("  delivery: stored on the relay; the guest reads it on the share's page")
                click.echo(f"  from:     {me.address if me else origin}")
                return
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
                click.echo(f"  delivery: {'fyi' if fyi else 'push'} through the relay to that machine's bridge"
                           + (", held there until the agent is idle" if when == "idle" else ""))
                click.echo(f"  from:     {me.address if me else origin}")
                _echo_images(images)
                return
            if not yes:
                session = router.resolve(target, exclude=me)
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {session.address}  {session.title or session.cwd}")
                click.echo(f"  delivery: {'fyi (feed inbox)' if fyi else session.delivery} ({session.status or 'unknown'})")
                if when == "idle" and not fyi and held.is_busy(session):
                    click.echo(f"  when:     {held.held_label(session.address)}")
                click.echo(f"  from:     {me.address if me else origin}")
                _echo_images(images)
                return
            receipt = router.send(target, message, me, origin, fyi=fyi,
                                  reply="none" if no_reply else "expected", images=images, tags=tags, when=when,
                                  wait_reply=not no_wait)
        except (router.LiveError, names.NameRefused, share.ShareError, RuntimeError, OSError) as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps(receipt))
        else:
            click.echo(f"{receipt['status']}: {receipt['to']}")
            if receipt.get("reply") is not None:
                click.echo(receipt["reply"])
        # Notes to user and answers to a guest have no delivery to follow; the relay tracks deliveries to agents only.
        if ((receipt.get("via") == "hub" and receipt["to"] != router.USER and receipt.get("delivery") != "guest")
                or receipt.get("delivery") == "held") \
                and not as_json:
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

    @live.command("inbox", context_settings=HELP, epilog=examples(
        "one-conv live inbox", "one-conv live inbox --tag deploy", "one-conv live inbox --all --limit 20",
        "one-conv live send user 'release notes drafted' --tag release --yes"))
    @click.option("--tag", "tags", multiple=True, metavar="TAG", help="Only notes carrying this tag (repeatable: any of them).")
    @click.option("--all", "show_all", is_flag=True, help="Show the latest notes, read or not, without moving the read position.")
    @click.option("--peek", is_flag=True, help="Show unread notes without marking them read.")
    @click.option("--limit", default=50, show_default=True, help="Most notes to show.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the notes as JSON.")
    def inbox_command(tags, show_all, peek, limit, as_json):
        """Read the notes agents (or you) sent to `user`, oldest unread first, and mark them read.

        Notes live on the relay when this machine has one, so every machine shows the same
        inbox; otherwise on this machine. Each tag filter keeps its own read position, so
        reading `--tag deploy` leaves the other notes unread.
        """
        try:
            tags = router.check_tags(list(tags))
            source_key = "relay" if hubclient.configured() else "local"
            position = _inbox_position_key(source_key, tags)
            positions = _inbox_positions()
            after = positions.get(position, 0)
            page = router.latest_notes(tags, limit) if show_all else router.user_notes(after, tags, limit)
        except (router.LiveError, hubclient.HubRefused) as error:
            raise click.ClickException(str(error)) from None
        notes = page["messages"]
        if not show_all and not peek:
            # A full page may stop short of the newest note; otherwise everything up to `last` was seen.
            seen = notes[-1]["seq"] if len(notes) >= limit else max(page["last"], after)
            if seen != after:
                positions[position] = seen
                _save_inbox_positions(positions)
        if as_json:
            click.echo(json.dumps(notes, ensure_ascii=False))
            return
        if not notes:
            click.echo("No unread notes." if not show_all else "No notes.", err=True)
            return
        for note in notes:
            when = time.strftime("%m-%d %H:%M", time.localtime(note.get("created") or 0))
            who = note["from"] + (f" ({note['name']})" if note.get("name") else "")
            labels = f"  [{', '.join(note['tags'])}]" if note.get("tags") else ""
            click.echo(f"#{note['seq']}  {when}  {who}{labels}")
            for line in (note.get("text") or "").splitlines() or [""]:
                click.echo(f"  {line}")

    def _inbox_positions_path():
        from .model import live_home
        return live_home() / "user" / "read.json"

    def _inbox_position_key(source: str, tags: list[str]) -> str:
        return " ".join([source, *sorted(tags)])

    def _inbox_positions() -> dict:
        try:
            positions = json.loads(_inbox_positions_path().read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return positions if isinstance(positions, dict) else {}

    def _save_inbox_positions(positions: dict) -> None:
        path = _inbox_positions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".read.{os.getpid()}.tmp")
        partial.write_text(json.dumps(positions, indent=1, sort_keys=True))
        os.replace(partial, path)

    @live.command("status", context_settings=HELP, epilog=examples(
        "one-conv live status 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e"))
    @click.argument("message_id")
    @click.option("--json", "as_json", is_flag=True, help="Emit the outcome as JSON.")
    def status_command(message_id, as_json):
        """Show what became of a held message or one sent through the relay, by its 32-character id.

        The outcome per recipient is what that machine's harness did (accepted, refused,
        unconfirmed), held while the agent is busy, or stored while it has not been handed
        over yet. A message held on this machine shows `held until ... is idle`, then
        delivered, unconfirmed or expired.
        """
        if not HUB_MESSAGE_ID.fullmatch(message_id):
            raise click.UsageError("MESSAGE_ID is the 32-character id a held send or a send through the relay printed.")
        kept = held.lookup(message_id)
        if kept is not None:
            if as_json:
                click.echo(json.dumps({key: kept[key] for key in ("id", "target", "status", "detail", "held", "finished")}))
            elif kept["status"] == "held":
                click.echo(f"{kept['detail']}")
            else:
                click.echo(f"{kept['status']}: {kept['target']} ({kept['detail']})")
            return
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
            click.echo(held.held_label(row["to"]) if row["outcome"] == "held" else f"{row['outcome']}: {row['to']}")

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
        """Register the MCP server, omp extension, Cursor hook and agy hooks that live relaying needs.

        These reach sessions on this machine. Reaching other machines also needs the bridge:
        `live join` with an invite from a connected machine, or `live serve --hub ...`.
        """
        from . import bridge
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
        if not bridge.settings().get("hub"):
            click.echo("No relay yet: to reach other machines, run the `one-conv live join ...` command that"
                       " `one-conv live invite NODE` prints on a connected machine.")

    @live.command("spawn", context_settings={**HELP, "ignore_unknown_options": True},
                  epilog=examples("one-conv live spawn codex --cwd ../review --brief-file brief.md --yes",
                                  "one-conv live spawn cursor --cwd ../review --brief 'review PR 632' --yes --json",
                                  "one-conv live spawn claude --mode auto --brief-file brief.md --yes",
                                  "one-conv live spawn claude --brief-file - --yes -- --model opus < brief.md"))
    @click.argument("harness", type=click.Choice(spawn.SPAWNABLE))
    @click.argument("harness_args", nargs=-1, type=click.UNPROCESSED)
    @click.option("--cwd", default=".", type=click.Path(exists=True, file_okay=False, resolve_path=True),
                  help="Folder the agent works in. Default: the current one.")
    @click.option("--brief", help="The agent's first prompt.")
    @click.option("--brief-file", type=click.File("r"), help="Read the first prompt from a file, or - for stdin.")
    @click.option("--mode", type=click.Choice(spawn.MODES), default="ask", show_default=True,
                  help="Shell and edit approvals: ask (your harness settings), auto (the harness's reviewed "
                       "auto mode), yolo (no prompts).")
    @click.option("--session", "tmux_session", help="tmux session name. Default: <harness>-<folder>.")
    @click.option("--timeout", default=120.0, show_default=True, help="Seconds to wait for the agent to register.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    @click.option("--yes", is_flag=True, help="Start it. Without it, only prints the command.")
    def spawn_command(harness, harness_args, cwd, brief, brief_file, mode, tmux_session, timeout, as_json, yes):
        """Start an agent in a detached tmux session and return its live address.

        The brief is its first prompt, headed by your address so it reports back with
        `send_message`. The one-conv tools are pre-approved; its shell and edit prompts
        follow --mode. Arguments after `--` go to the harness unchanged.
        Exits non-zero, showing the agent's screen, when it does not register in time,
        for example while it waits on a prompt: answer it with `tmux attach -t <session>`.
        """
        text = brief if brief is not None else (brief_file.read() if brief_file else "")
        if not text.strip():
            raise click.UsageError("Give the agent a brief with --brief or --brief-file: it starts working on it.")
        sender = router.whoami()
        text = spawn.with_reply_path(text, sender)
        try:
            command = spawn.launch(harness, text, cwd, list(harness_args), mode)
        except ValueError as error:
            raise click.UsageError(str(error)) from None
        name = tmux_session or spawn.tmux_name(harness, cwd, spawn.tmux_taken)
        if not yes:
            click.echo(f"would start in tmux session {name} ({cwd}):\n  {shlex.join(command.argv)}")
            click.echo("Re-run with --yes to start it.")
            return
        adapter = router.ADAPTERS[harness]
        before = {session.id for session in adapter.list_sessions()}
        try:
            spawn.start(name, cwd, command)
        except (RuntimeError, OSError) as error:
            raise click.ClickException(str(error)) from None
        # Recorded before registering, so an agent stuck on a prompt can still be stopped by its tmux name.
        row = spawn.Spawned(name, harness, cwd, "", sender.address if sender else "", time.time())
        spawn.record(row)
        session = spawn.wait_for(adapter.list_sessions, before, cwd, timeout, lambda: spawn.tmux_taken(name))
        if session is None:
            state = "is still running" if spawn.tmux_taken(name) else "exited"
            raise click.ClickException(
                f"{harness} {state} but did not register within {timeout:g}s. Its screen:\n{spawn.screen(name)}\n"
                f"Attach with: tmux attach -t {name}, or stop it: one-conv live stop {name} --yes")
        row.address = session.address
        spawn.record(row)
        if as_json:
            click.echo(json.dumps({**session.to_json(), "tmux": name}))
            return
        click.echo(f"started {session.address} in tmux session {name} (attach: tmux attach -t {name}, "
                   f"stop: one-conv live stop {session.address} --yes)")

    @live.command("stop", context_settings=HELP,
                  epilog=examples("one-conv live stop", "one-conv live stop codex:019a0c5e --yes",
                                  "one-conv live stop @reviewer --yes", "one-conv live stop codex-review --yes",
                                  "one-conv live stop --all --yes"))
    @click.argument("targets", nargs=-1)
    @click.option("--all", "stop_all", is_flag=True, help="Every agent `live spawn` started on this machine.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    @click.option("--yes", is_flag=True, help="Stop them. Without it, only lists what would stop.")
    def stop_command(targets, stop_all, as_json, yes):
        """End agents started with `live spawn`, by address, @name, id prefix or tmux session.

        Kills the agent's tmux session; its conversation stays readable with `one-conv local`.
        Without a target, lists the spawned agents. Sessions `live spawn` did not start
        are refused: stop those yourself.
        """
        registry = spawn.load_registry()
        if not targets and not stop_all:
            rows = [{**vars(row), "running": spawn.tmux_taken(row.tmux)} for row in registry.values()]
            if as_json:
                click.echo(json.dumps(rows))
                return
            if not rows:
                click.echo("No agent started with `live spawn` on this machine.")
            for row in rows:
                state = "running" if row["running"] else "ended"
                click.echo(f"  {row['tmux']:<28} {row['address'] or '(never registered)':<52} {state}  {row['cwd']}")
            return
        chosen: dict[str, spawn.Spawned] = dict(registry) if stop_all else {}
        for target in targets:
            try:
                resolved = router.resolve(target).address
            except router.LiveError:
                resolved = None
            matches = spawn.find(target, registry, resolved)
            if not matches:
                raise click.ClickException(f"{target} was not started by `live spawn` on this machine: stop it yourself. "
                                           "Run `one-conv live stop` to list the agents it can stop.")
            if len(matches) > 1:
                listing = "\n".join(f"  {row.tmux}  {row.address}" for row in matches)
                raise click.ClickException(f"{target!r} matches several spawned agents:\n{listing}")
            chosen[matches[0].tmux] = matches[0]
        if not yes:
            for row in chosen.values():
                click.echo(f"would stop {row.address or '(never registered)'} (tmux session {row.tmux})")
            click.echo("Re-run with --yes to stop them.")
            return
        results = []
        for row in chosen.values():
            try:
                was_running = spawn.stop(row)
            except RuntimeError as error:
                raise click.ClickException(str(error)) from None
            results.append({**vars(row), "was_running": was_running})
            if not as_json:
                click.echo(f"{'stopped' if was_running else 'already ended'} {row.address or row.tmux} (tmux session {row.tmux})")
        if as_json:
            click.echo(json.dumps(results))

    @live.command("teleport", context_settings=HELP,
                  epilog=examples("one-conv live teleport studio --check",
                                  "one-conv live teleport studio --yes",
                                  "one-conv live teleport studio --push-wip --yes --json",
                                  "one-conv live teleport ci-runner --cwd '~/code/app' --mode auto --yes"))
    @click.argument("node")
    @click.option("--cwd", help="Folder on NODE to work in, instead of the clone found there by its remote URL.")
    @click.option("--mode", type=click.Choice(spawn.MODES),
                  help="Approvals on NODE. Default: the session's own mode when its transcript says, else ask.")
    @click.option("--check", is_flag=True, help="Only show what NODE has and lacks; move nothing.")
    @click.option("--force", is_flag=True, help="Go ahead although NODE lacks a tool, MCP server, skill or dotfile.")
    @click.option("--push-wip", is_flag=True,
                  help="Push committed but unpushed work (HEAD) to a teleport/<id> branch so NODE can fetch it.")
    @click.option("--from", "sender_address", help="The session to move; detected from the calling agent by default.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
    @click.option("--yes", is_flag=True, help="Move the session. Without it, only checks.")
    def teleport_command(node, cwd, mode, check, force, push_wip, sender_address, as_json, yes):
        """Move the calling agent session to machine NODE on the relay, where it continues its task.

        Takes an inventory of what the session uses (harness and model, git checkout, CLIs,
        MCP servers, skills, dotfiles it read under ~), asks NODE's bridge which of those it has,
        and prints ok / missing / unknown for each. With --yes and nothing blocking, it syncs this
        session to the archive and has NODE start the same harness in tmux, in its clone of the
        repository at the same commit (a new worktree when that clone is elsewhere), briefed with
        this conversation. The new session's first act is a message back here confirming; this
        session must then stop working. Uncommitted changes always refuse; commits only on this
        machine refuse unless --push-wip. NODE must opt in: `one-conv live serve --accept-teleport`.
        """
        from . import inventory as inventories, teleport
        if transcript is None:
            raise click.ClickException("teleport needs the history readers of the one-conv CLI")
        try:
            sender = router.whoami(sender_address)
        except router.LiveError as error:
            raise click.UsageError(str(error)) from None
        if sender is None or sender.id.startswith("?"):
            raise click.UsageError("Run this from inside the agent session to move, or pass --from ADDRESS.")
        try:
            read = transcript(sender.address)
        except click.ClickException as error:
            raise click.ClickException(f"cannot read {sender.address}'s transcript: {error.message}") from None
        found = inventories.build(sender.harness, sender.cwd or read.get("cwd") or "", read.get("blocks") or [],
                                  read.get("path"))
        try:
            report = teleport.run(node, sender, read, found, cwd=cwd, mode=mode, check_only=check or not yes,
                                  force=force, wip=push_wip)
        except (teleport.TeleportError, hubclient.HubRefused) as error:
            if as_json:
                click.echo(json.dumps({"status": "failed", "error": str(error),
                                       **({"screen": error.screen} if getattr(error, "screen", "") else {})}))
                sys.exit(1)
            screen = getattr(error, "screen", "")
            raise click.ClickException(f"{error}" + (f"\nIts screen on {node}:\n{screen}" if screen else "")) from None
        if as_json:
            click.echo(json.dumps(report))
            if report["status"] == "refused":
                sys.exit(1)
            return
        click.echo(f"{report['origin']} -> {node}" + (f" ({report['dir']})" if report.get("dir") else ""))
        for line in teleport.table(report["rows"]):
            click.echo(line)
        if report["status"] == "refused":
            raise click.ClickException("not teleporting:\n" + "\n".join(f"  - {reason}" for reason in report["blockers"]))
        if report["status"] == "checked":
            click.echo("Nothing blocks this teleport." + ("" if check else " Re-run with --yes to move the session."))
            return
        click.echo(f"teleported: {report['address']} in {report['dir']} at {report['branch']}@{report['sha'][:12]}"
                   if report.get("sha") else f"teleported: {report['address']} in {report['dir']}")
        if report.get("name_error"):
            click.echo(f"warning: the name did not move: {report['name_error']}", err=True)
        click.echo(report["next"])

    @live.command("cursor-hook", hidden=True, epilog=examples('one-conv live cursor-hook < hook-payload.json'))
    def cursor_hook_command():
        """Cursor `stop` hook entry point: reads the hook payload on stdin."""
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        click.echo(json.dumps(cursor.run_stop_hook(payload)))

    @live.command("agy", context_settings={**HELP, "ignore_unknown_options": True, "allow_extra_args": True},
                  epilog=examples('one-conv live agy', 'one-conv live agy -- --model gemini-3.8-flash-high',
                                  'one-conv live agy -- --conversation 0a9e7c1d-0000-4000-8000-000000000001'))
    @click.argument("agy_args", nargs=-1, type=click.UNPROCESSED)
    def agy_command(agy_args):
        """Start agy so messages reach it at once, even while it waits for you.

        Arguments after `--` go to agy unchanged. An agy started any other way receives
        messages through its hooks, before its next model call or when a turn ends.
        """
        argv, env = agy.launch_command(list(agy_args))
        try:
            os.execvpe(argv[0], argv, env)
        except FileNotFoundError:
            raise click.ClickException("agy is not on PATH; install the Antigravity CLI first.") from None

    @live.command("agy-hook", hidden=True, epilog=examples('one-conv live agy-hook < hook-payload.json'))
    def agy_hook_command():
        """agy hook entry point for tests and debugging; installed hooks run agy_hook.py directly."""
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        click.echo(json.dumps(agy.run_hook(payload if isinstance(payload, dict) else {}, os.getppid(),
                                           os.environ.get(agy.TOKEN_ENV))))

    from .bridge_cli import add_bridge_commands
    add_bridge_commands(live, launcher)
    from .share_cli import add_share_command
    add_share_command(live)

    return live
