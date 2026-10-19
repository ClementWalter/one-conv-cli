"""`one-conv live`: list running agent sessions and message them while they run."""

import json
import re
import sys
import time

import click

from . import cursor, hubclient, router, team
from . import install as installer
from .model import HARNESSES

HELP = {"help_option_names": ["-h", "--help"]}
# The id the sender chose for a message sent through the hub.
HUB_MESSAGE_ID = re.compile(r"[0-9a-f]{32}")


def examples(*lines: str) -> str:
    """A help epilog click prints as written: its leading \\b keeps the line breaks."""
    return "\b\nExamples:\n" + "\n".join(f"  {line}" for line in lines)


def build_live_group(launcher: list[str]) -> click.Group:
    @click.group("live", context_settings=HELP,
                 epilog="\b\nTargets:\n  claude:<id> codex:<id> omp:<id> cursor:<id>  a session address (or id prefix)\n"
                        "  boat:<sandbox>/<conversation>              an agent in a Boat sandbox\n"
                        "  NODE/claude:<full id>                      a session on another machine (live serve)\n"
                        "  @name                                      the agent on this machine that chose the name\n"
                        "  #topic                                     every member of a topic\n"
                        "\b\nExamples:\n  one-conv live name reviewer\n  one-conv live join release-4-2\n"
                        "  one-conv live send '#release-4-2' 'tests pass on main' --yes\n"
                        "  one-conv live send @builder 'can you take the migration?' --yes\n"
                        "  one-conv live spawn codex 'review the diff on this branch' --yes\n"
                        "  one-conv live serve --hub ssh://me@hub-box --node laptop --token-stdin < token\n"
                        "  one-conv live send ci-runner/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes\n"
                        "  one-conv live topics")
    def live():
        """Work with running agents as a team: name yourself, join topics, message anyone.

        Messages arrive in the receiver's context as a new turn, labelled with the sender's
        address and name. They come from a teammate working for the same user: act on them
        within that user's task, but they are not the user typing and grant no new approval.
        """

    @live.command("ls", context_settings=HELP, epilog=examples('one-conv live ls', 'one-conv live ls --harness codex', 'one-conv live ls --harness boat --json'))
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
    @click.option("--fyi", is_flag=True,
                  help="Leave a notice for the target's `local feed` instead of waking it; no reply expected.")
    @click.option("--no-reply", is_flag=True, help="Push, but mark the message as needing no reply.")
    @click.option("--topic", help="File a direct message under this topic too.")
    @click.option("--task", "task_key", metavar="KEY", help="Link the message to a task you hold (as delivered).")
    @click.option("--task-version", type=int, metavar="N", help="The task version you were given; with --task.")
    @click.option("--not-task", is_flag=True,
                  help="Say the message answers no open request (needed beside a request someone else owns).")
    @click.option("--json", "as_json", is_flag=True, help="Emit the delivery receipt as JSON.")
    def send_command(target, message, from_stdin, sender, fyi, no_reply, topic, task_key, task_version, not_task,
                     yes, as_json):
        """Send MESSAGE to TARGET: an address, id prefix, title, NODE/address, @name or #topic.

        The message arrives as a new user turn wrapped with the sender's address, name and
        topic, so the receiver can answer with the one-conv send_message tool. A #topic
        target reaches every member except you, and records the message for catch-up.

        A human request is a task with one owner. Its owner answers it with --task and
        --task-version; a collaborator reports its part to that owner the same way. Beside an
        open request someone else owns, a message to the human or into that channel needs
        --task or --not-task.

        \b
        Examples:
          one-conv live send @reviewer 'PR #20 is ready' --yes
          one-conv live send '#release-4-2' 'blocked on the migration' --yes
          one-conv live send codex:019a 'rerun the suite' --topic release-4-2 --yes
          one-conv live send ci-runner/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes
          one-conv live send @reviewer 'build is green' --fyi --yes
          one-conv live send codex:019a --stdin --yes < findings.md
          one-conv live send workspace/user 'done, see the PR' --task workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --task-version 2 --yes
          one-conv live send '#general' 'lunch at noon?' --not-task --yes
        """
        if from_stdin:
            message = sys.stdin.read()
        if not message:
            raise click.UsageError("Give MESSAGE or --stdin.")
        if (task_key is None) != (task_version is None):
            raise click.UsageError("--task and --task-version go together, as the delivery gave them:\n"
                                   "  one-conv live send TARGET 'text' --task KEY --task-version N --yes")
        if task_key and not_task:
            raise click.UsageError("Use --task or --not-task, not both.")
        link = {"key": task_key, "version": task_version} if task_key else None
        try:
            me = router.whoami(sender)
            origin = "user" if sys.stdin.isatty() and not from_stdin else "unknown"
            if not yes and router.topic_of(target) is not None:
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  topic:    #{router.topic_of(target)}")
                click.echo(f"  members:  {', '.join(team.members(router.topic_of(target))) or 'none yet'}")
                click.echo(f"  from:     {me.address if me else origin}")
                return
            if not yes:
                session = router.resolve(target, exclude=me)
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:       {session.address}  {session.title or session.cwd}")
                click.echo(f"  delivery: {'fyi (feed inbox)' if fyi else session.delivery} ({session.status or 'unknown'})")
                click.echo(f"  from:     {me.address if me else origin}")
                return
            receipt = router.send(target, message, me, origin, fyi=fyi,
                                  reply="none" if no_reply else "expected", topic=topic, task=link,
                                  about="other" if not_task else None)
        except (router.LiveError, team.TeamError, RuntimeError, OSError) as error:
            raise click.ClickException(str(error)) from None
        if receipt.get("task") and not as_json:
            task = receipt["task"]
            if task.get("owner"):
                click.echo(f"task {task['key']}: owned by {task['owner']}; the others received it as context")
            else:
                click.echo(f"task {task['key']}: unclaimed, offered to every running member of the channel")
        if as_json:
            click.echo(json.dumps(receipt))
        elif "deliveries" in receipt:
            click.echo(f"#{receipt['topic']}: {len(receipt['deliveries'])} member(s)")
            for row in receipt["deliveries"]:
                click.echo(f"  {row['status']}: {row['to']}")
        else:
            click.echo(f"{receipt['status']}: {receipt['to']}")
        if receipt.get("via") == "hub" and not as_json:
            click.echo(f"  follow it with: one-conv live status {receipt['id']}")
        if receipt.get("warning") and not as_json:
            click.echo(f"warning: {receipt['warning']}", err=True)

    def _me(address):
        me = router.whoami(address)
        if me is None or me.id.startswith("?"):
            raise click.ClickException("This command acts as the calling agent, which could not be identified; "
                                       "pass --as harness:id, e.g. --as codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03")
        return me.address

    def _show_task(task, as_json):
        if as_json:
            click.echo(json.dumps(task, ensure_ascii=False))
            return
        owner = task.get("owner") or "nobody"
        click.echo(f"{task['key']}  v{task['version']}  {task['state']}  owner {owner}  {task.get('kind', '')}")
        if task.get("title"):
            click.echo(f"  request: {task['title']}")
        if task.get("note"):
            click.echo(f"  note: {task['note']}")
        for child in task.get("children", []):
            click.echo(f"  child {child['key']}  {child['kind']}  {child['state']}  {child.get('owner') or 'nobody'}")

    def _task_call(work):
        from . import taskops
        try:
            return work(taskops)
        except taskops.TaskOpError as error:
            current = f"\n  now: {error.task['state']}, owner {error.task.get('owner') or 'nobody'}, " \
                      f"version {error.task['version']}" if error.task else ""
            raise click.ClickException(f"{error}{current}") from None

    @live.group("task", context_settings=HELP, epilog=examples(
        "one-conv live task list --mine --open", "one-conv live task show workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7",
        "one-conv live task update workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --state working --note 'reading the logs'",
        "one-conv live task delegate workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --to @reviewer --kind review"))
    def task_group():
        """Work a human request you own: report progress, grant parts to teammates, finish it.

        A human request is a task with one owner, who alone works it and answers it. The
        others receive it as context. Every owner action carries the version you were given,
        so an agent the task was taken from is refused instead of acting on it.
        """

    @task_group.command("list", context_settings=HELP, epilog=examples(
        "one-conv live task list", "one-conv live task list --mine --open", "one-conv live task list --topic general --json"))
    @click.option("--topic", help="Only this topic's tasks.")
    @click.option("--mine", is_flag=True, help="Only tasks the calling agent owns.")
    @click.option("--open", "open_only", is_flag=True, help="Only tasks not done or cancelled.")
    @click.option("--as", "address", help="Act as this session (harness:id); detected by default.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def task_list_command(topic, mine, open_only, address, as_json):
        """List tasks, newest first."""
        owner = None
        if mine:
            owner = _me(address)
            hub = hubclient.configured()
            if hub is not None and hub.node:
                owner = f"{hub.node}/{owner}"
        found = _task_call(lambda ops: ops.listing(topic, owner, open_only))
        if as_json:
            click.echo(json.dumps(found, ensure_ascii=False))
            return
        if not found:
            click.echo("No tasks.")
        for task in found:
            click.echo(f"{task['key']}  v{task['version']}  {task['state']:<10}  {task.get('owner') or 'nobody':<40}  "
                       f"{task.get('title', '')[:60]}")

    @task_group.command("show", context_settings=HELP, epilog=examples(
        "one-conv live task show workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7",
        "one-conv live task show workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --json"))
    @click.argument("key")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def task_show_command(key, as_json):
        """Show a task: owner, state, version, note, child tasks."""
        _show_task(_task_call(lambda ops: ops.show(key)), as_json)

    @task_group.command("claim", context_settings=HELP, epilog=examples(
        "one-conv live task claim workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7",
        "one-conv live task claim workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 1 --json"))
    @click.argument("key")
    @click.option("--version", type=int, help="Claim only at this version.")
    @click.option("--as", "address", help="Act as this session (harness:id); detected by default.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def task_claim_command(key, version, address, as_json):
        """Take an unassigned task, or confirm one you own. Never takes a task from its owner.

        Retrying is safe: a claim you already hold answers with the task as it is. If the
        answer is lost, run it again rather than guessing.
        """
        _show_task(_task_call(lambda ops: ops.claim(key, _me(address), version)), as_json)

    @task_group.command("update", context_settings=HELP, epilog=examples(
        "one-conv live task update workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --state working --note 'reading the logs'",
        "one-conv live task update workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --state blocked --note 'need the API key'",
        "one-conv live task update workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --state done --note 'answered in #general'"))
    @click.argument("key")
    @click.option("--version", type=int, required=True, help="The version you were given.")
    @click.option("--state", type=click.Choice(["working", "blocked", "done", "cancelled"]), help="New state.")
    @click.option("--note", help="A short progress note the human sees.")
    @click.option("--as", "address", help="Act as this session (harness:id); detected by default.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def task_update_command(key, version, state, note, address, as_json):
        """Report progress on a task you own: working, blocked, done or cancelled, with a note."""
        if state is None and note is None:
            raise click.UsageError("Give --state, --note, or both:\n  one-conv live task update KEY --version N "
                                   "--state working --note 'what you are doing'")
        _show_task(_task_call(lambda ops: ops.update(key, _me(address), version, state, note)), as_json)

    @task_group.command("delegate", context_settings=HELP, epilog=examples(
        "one-conv live task delegate workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --to @reviewer --kind review",
        "one-conv live task delegate workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --to @builder --kind implementation --note 'the migration only'",
        "one-conv live task delegate workspace:9f2c0a1b2c3d4e5f60718293a4b5c6d7 --version 2 --to @reviewer --kind review --round 2"))
    @click.argument("key")
    @click.option("--version", type=int, required=True, help="The version you were given.")
    @click.option("--to", "to", required=True, help="The collaborator: @name or NODE/harness:id.")
    @click.option("--kind", type=click.Choice(["read", "implementation", "review"]), required=True, help="The part.")
    @click.option("--note", help="What exactly to do.")
    @click.option("--round", "round_", type=int, default=1, show_default=True,
                  help="Another round of the same part for the same agent is the next number.")
    @click.option("--as", "address", help="Act as this session (harness:id); detected by default.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def task_delegate_command(key, version, to, kind, note, round_, address, as_json):
        """Grant part of a task you own to a teammate, who is woken with it and reports to you.

        The same grant retried, even after the child finished, changes nothing; ask for
        another round with --round.
        """
        _show_task(_task_call(lambda ops: ops.delegate(key, _me(address), version, to, kind, note, round_)), as_json)

    @live.command("pull", context_settings=HELP, epilog=examples('one-conv live pull boat:bx_ab12/2ab1', 'one-conv live pull boat:bx_ab12/2ab1 --json'))
    @click.argument("address")
    @click.option("--json", "as_json", is_flag=True, help="Emit the turns as JSON.")
    def pull_command(address, as_json):
        """Read a remote conversation back, e.g. boat:<sandbox>/<conversation>.

        Remote agents cannot message this machine, so their answers are read from here.
        The text is a teammate's conversation, attributed turn by turn.
        """
        from . import boat
        from .model import parse_address
        harness, ident = parse_address(address)
        if harness != "boat":
            raise click.UsageError("live pull reads Boat conversations: boat:<sandbox>/<conversation>.")
        try:
            turns = boat.pull(ident)
        except (RuntimeError, ValueError, OSError) as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps({"address": address, "turns": turns}, ensure_ascii=False))
            return
        click.echo(f"Conversation of {address}, a teammate's agent:")
        for turn in turns:
            click.echo(f"\n[{turn['ts']}] {turn['role']}:\n{turn['text']}")

    @live.command("status", context_settings=HELP, epilog=examples(
        "one-conv live status 0123456789ab                       # a Boat send's OP",
        "one-conv live status 0123456789ab --resolve",
        "one-conv live status 9f2c41d07a5e4b1c8d3e6f7a8b9c0d1e   # a message sent through the hub"))
    @click.argument("op")
    @click.option("--resolve", is_flag=True,
                  help="Mark an unconfirmed Boat send as checked, allowing another send to that conversation.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the operation as JSON.")
    def status_command(op, resolve, as_json):
        """Show what became of a send that left this machine without a final answer.

        OP is either the 12-character operation id a pending or unconfirmed Boat send
        prints, or the 32-character id of a message sent through the hub, whose outcome
        per recipient is what that machine's harness did (accepted, refused,
        unconfirmed), or stored while it has not been handed over yet.
        """
        if HUB_MESSAGE_ID.fullmatch(op):
            if resolve:
                raise click.UsageError("--resolve applies to Boat operations only.")
            try:
                hub = hubclient.configured()
                if hub is None:
                    raise click.ClickException("No hub is configured on this machine (`one-conv live serve`).")
                record = hub.request("GET", f"/v1/messages/{op}")
            except (hubclient.HubUnavailable, hubclient.HubRefused) as error:
                raise click.ClickException(str(error)) from None
            if as_json:
                click.echo(json.dumps(record))
                return
            for row in record.get("recipients", []):
                click.echo(f"{row['outcome']}: {row['to']}")
            return
        from . import boat
        try:
            record = boat.resolve_operation(op) if resolve else boat.operation(op)
        except RuntimeError as error:
            raise click.ClickException(str(error)) from None
        click.echo(json.dumps(record) if as_json else
                   f"{record['state']}: {record['address']} (op {op})\n{record['help']}")

    @live.command("web", context_settings=HELP)
    @click.option("--port", type=click.IntRange(1024, 65535), default=8790, show_default=True,
                  help="Port on 127.0.0.1.")
    def web_command(port):
        """Open a workspace to follow topics and direct messages, and send as a human.

        It listens on 127.0.0.1 only and has no login, so anyone who can use this machine
        can read it and send messages as You. Viewing topics never marks agents' messages read.

        \b
        Example:
          one-conv live web            # then open http://127.0.0.1:8790
        """
        import os
        import shutil
        from pathlib import Path
        script = Path(__file__).resolve().parents[2] / "scripts" / "live-web.py"
        uv = shutil.which("uv")
        if uv is None:
            raise click.ClickException("`uv` is needed to run the viewer: https://docs.astral.sh/uv/")
        os.execv(uv, [uv, "run", "--quiet", "--script", str(script), "--port", str(port)])

    @live.command("whoami", context_settings=HELP, epilog=examples('one-conv live whoami'))
    def whoami_command():
        """Show which session this process is running inside, as other sessions see it."""
        me = router.whoami()
        if me is None:
            click.echo("user (not inside an agent session)")
            return
        name = team.name_for(me.address)
        click.echo(f"{me.address}  @{name}" if name else me.address)

    def _me(as_address):
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

        Messages still route and reply by address, so renaming never breaks a reply.
        """
        try:
            chosen = team.set_name(name, _me(as_address).address, _live, replace)
        except (team.TeamError, router.LiveError) as error:
            raise click.ClickException(str(error)) from None
        click.echo(f"you are @{chosen}")

    @live.command("names", context_settings=HELP, epilog=examples('one-conv live names', 'one-conv live names --json'))
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def names_command(as_json):
        """List agent names and the addresses they stand for."""
        table = team.names()
        if as_json:
            click.echo(json.dumps(table))
            return
        for name, address in sorted(table.items()):
            click.echo(f"@{name}  {address}")

    def _print_catch_up(result):
        if not result["messages"]:
            click.echo(f"#{result['topic']}: nothing new")
            return
        if result["omitted"]:
            click.echo(f"({result['omitted']} older unseen message(s) not shown; --all shows the full history)")
        click.echo(f"History of #{result['topic']}, already exchanged (not new requests):")
        for row in result["messages"]:
            name = team.name_for(row["from"])
            who = f"@{name} {row['from']}" if name else row["from"]
            if row.get("to"):
                who += f" → {row['to']} only"
            click.echo(f"\n[{time.strftime('%Y-%m-%d %H:%M', time.localtime(row['at']))}] {who}:\n{row['text']}")

    @live.command("join", context_settings=HELP, epilog=examples('one-conv live join release-4-2', 'one-conv live join release-4-2 --as codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03'))
    @click.argument("topic")
    @click.option("--as", "as_address", help="Session that joins; defaults to the calling agent.")
    def join_command(topic, as_address):
        """Join TOPIC (creating it if needed) and catch up on what was exchanged there.

        Members receive every message sent to #TOPIC from then on.
        """
        try:
            me = _me(as_address)
            joined = team.join(topic, me.address)
            result = team.catch_up(joined, me.address)
        except (team.TeamError, router.LiveError) as error:
            raise click.ClickException(str(error)) from None
        click.echo(f"joined #{joined} ({len(team.members(joined))} member(s))")
        _print_catch_up(result)

    @live.command("leave", context_settings=HELP, epilog=examples('one-conv live leave release-4-2'))
    @click.argument("topic")
    @click.option("--as", "as_address", help="Session that leaves; defaults to the calling agent.")
    def leave_command(topic, as_address):
        """Stop receiving messages sent to TOPIC."""
        try:
            left = team.leave(topic, _me(as_address).address)
        except (team.TeamError, router.LiveError) as error:
            raise click.ClickException(str(error)) from None
        click.echo(f"left #{left}")

    @live.command("topic", context_settings=HELP, epilog=examples('one-conv live topic release-4-2', 'one-conv live topic release-4-2 --all --peek'))
    @click.argument("topic")
    @click.option("--as", "as_address", help="Reader; defaults to the calling agent.")
    @click.option("--all", "everything", is_flag=True, help="Show the whole kept history, not only unseen messages.")
    @click.option("--peek", is_flag=True, help="Do not mark the shown messages as seen.")
    def topic_command(topic, as_address, everything, peek):
        """Show what was exchanged on TOPIC that you have not seen yet."""
        try:
            me = router.whoami(as_address)
            reader = me.address if me else "user"
            result = team.catch_up(topic, reader, everything, peek)
        except (team.TeamError, router.LiveError) as error:
            raise click.ClickException(str(error)) from None
        _print_catch_up(result)

    @live.command("topics", context_settings=HELP, epilog=examples('one-conv live topics', 'one-conv live topics --json'))
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def topics_command(as_json):
        """List topics with their members and last activity."""
        rows = team.topics()
        if as_json:
            click.echo(json.dumps(rows))
            return
        if not rows:
            click.echo("No topics yet. `one-conv live join NAME` starts one.")
        for row in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["last"])) if row["last"] else "-"
            members = ", ".join(f"@{team.name_for(m)}" if team.name_for(m) else m for m in row["members"])
            click.echo(f"#{row['topic']}  {when}  {row['messages']} msg  {members}")

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

    @live.command("cursor-hook", hidden=True)
    def cursor_hook_command():
        """Cursor `stop` hook entry point: reads the hook payload on stdin."""
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        click.echo(json.dumps(cursor.run_stop_hook(payload)))

    from ..control.cli import build_control_group
    from .bridge_cli import add_bridge_commands
    from .spawn_cli import add_spawn_commands
    add_bridge_commands(live)
    add_spawn_commands(live)
    live.add_command(build_control_group())

    return live
