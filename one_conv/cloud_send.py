"""Post a one-conv message into a saved Claude chat as the account that owns it.

The message is wrapped in the same `<one-conv-message>` envelope live messages use,
so the chat sees who sent it. The reply comes back in that chat, and the command
returns it. It is a dry run unless `--yes` is given.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import click

from .cloud import cache_root


def saved_conversation(query: str, source: str, root: Path | None = None) -> dict:
    """The saved conversation QUERY names: an ID prefix (displayed or provider), else a title."""
    rows = []
    for path in (root or cache_root()).glob("*/*.json"):
        if path.name.startswith("."):
            continue
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("source") == source and document.get("id"):
            rows.append({field: document.get(field) for field in ("id", "session", "title", "account_id", "account_label")})
    folded = query.casefold()
    for match in (lambda row: row["session"].startswith(query) or row["id"].startswith(query),
                  lambda row: (row["title"] or "").casefold() == folded,
                  lambda row: folded in (row["title"] or "").casefold()):
        matches = [row for row in rows if match(row)]
        if matches:
            break
    else:
        raise click.ClickException(f"No saved {source} conversation matches {query!r}. Pull the account first.")
    if len(matches) != 1:
        choices = "; ".join(f"{row['session'][:12]} {row['title']}" for row in matches[:10])
        raise click.ClickException(f"Several conversations match {query!r}; use a longer ID: {choices}")
    return matches[0]


def local_timezone() -> str:
    """The IANA zone Claude dates the turn in, so the reply knows the sender's local time."""
    if os.environ.get("TZ", "").count("/") >= 1:
        return os.environ["TZ"].lstrip(":")
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def wrap(text: str, sender_address: str | None) -> str:
    """The live envelope, asking the chat to answer inline since it has no one-conv tools.

    The sender's working directory stays local: the chat may belong to someone else.
    """
    from dataclasses import replace
    from .live.model import envelope
    from .live import router

    sender = router.whoami(sender_address)
    origin = "user" if sender is None and sys.stdin.isatty() else "unknown"
    return envelope(text, None if sender is None else replace(sender, cwd=""), origin, reply="inline")


def send_command(name: str, product: str) -> click.Command:
    """`one-conv cloud claude send`: the account's own next turn in one of its chats."""

    @click.pass_context
    def invoke(context, conversation, message, from_stdin, sender, yes, as_json):
        from .cloud_cli import browser_provider
        from .live.router import LiveError
        from .providers.base import ProviderError

        if from_stdin:
            message = sys.stdin.read()
        if not message or not message.strip():
            raise click.UsageError("Give MESSAGE or --stdin.")
        row = saved_conversation(conversation, product)
        try:
            text = wrap(message, sender)
        except LiveError as error:
            raise click.ClickException(str(error)) from None
        account = row["account_label"] or row["account_id"]
        if not yes:
            click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
            click.echo(f"  to:      {row['title'] or row['id']}  ({account})")
            click.echo(f"  as:      the account itself; the chat replies to this turn")
            click.echo("  message:")
            click.echo("\n".join("    " + line for line in text.splitlines()))
            return
        try:
            provider = browser_provider(product, row["account_id"], context.obj["claude_accounts"])
            sent = provider.send_message(row["id"], text, timezone_name=local_timezone())
        except ProviderError as error:
            raise click.ClickException(f"{error}. Check the chat in the browser before resending.") from None
        receipt = {"conversation": row["id"], "title": row["title"], "account": account,
                   "message_id": sent.message_id, "reply_id": sent.reply_id,
                   "stop_reason": sent.stop_reason, "reply": sent.reply,
                   "url": f"https://claude.ai/chat/{row['id']}"}
        if as_json:
            click.echo(json.dumps(receipt, ensure_ascii=False))
        else:
            click.echo(f"sent: {receipt['title']} ({account})", err=True)
            click.echo(sent.reply)
        click.echo(f"Run one-conv cloud {name} pull --account {row['account_id']} to save it in history.", err=True)

    return click.Command(
        "send", callback=invoke,
        params=[click.Argument(["conversation"]), click.Argument(["message"], required=False),
                click.Option(["--stdin", "from_stdin"], is_flag=True, help="Read the message from stdin."),
                click.Option(["--from", "sender"], help="Sender address; detected from the calling agent by default."),
                click.Option(["--yes"], is_flag=True, help="Actually send. Without it, prints what would be sent."),
                click.Option(["--json", "as_json"], is_flag=True, help="Emit the receipt and reply as JSON.")],
        help=("Post MESSAGE into a saved chat as the account that owns it, and print Claude's reply.\n\n"
              "CONVERSATION is an ID prefix or title from `chats`. The message is wrapped in a "
              "<one-conv-message> envelope naming the sender. It is the account's own turn: the chat "
              "answers it, and the account's owner sees it in their history. Dry run without --yes."),
        epilog=(f"\b\nExamples:\n  one-conv cloud {name} send e08c3c85 'What did we settle on?'\n"
                f"  one-conv cloud {name} send 'Greek island stone house' --stdin --yes < note.md"),
        context_settings={"help_option_names": ["-h", "--help"]})
