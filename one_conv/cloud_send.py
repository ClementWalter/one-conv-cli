"""Deliver one-conv messages to saved Claude chats, as the account that owns each chat.

A Claude chat is addressed `claude-chat:<ID prefix or title>` in `live send` and the
`send_message` tool, next to local agent sessions; `cloud claude send` is the same
route. The message carries the live `<one-conv-message>` envelope. The chat has no
one-conv tools, so it answers in the conversation and the reply returns to the sender.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Callable, Iterable

import click

from .cloud import cache_root

CLAUDE_CHAT = "claude-chat:"
# Account discovery lives in the launcher (it reads browser logins and the vault), which registers it.
_accounts: Callable[[], Iterable[dict]] | None = None


class CloudSendError(RuntimeError):
    """A cloud chat could not be found or reached; the message may still have been posted."""


def register_accounts(discover: Callable[[], Iterable[dict]]) -> None:
    global _accounts
    _accounts = discover


def is_cloud_target(target: str) -> bool:
    return target.startswith(CLAUDE_CHAT)


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
        raise CloudSendError(f"No saved {source} conversation matches {query!r}. Pull the account first.")
    if len(matches) != 1:
        choices = "; ".join(f"{row['session'][:12]} {row['title']}" for row in matches[:10])
        raise CloudSendError(f"Several conversations match {query!r}; use a longer ID: {choices}")
    return matches[0]


def local_timezone() -> str:
    """The IANA zone Claude dates the turn in, so the reply knows the sender's local time."""
    if os.environ.get("TZ", "").count("/") >= 1:
        return os.environ["TZ"].lstrip(":")
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def cloud_envelope(text: str, sender, origin: str, reply: str = "expected",
                   message_id: str | None = None, topic: str | None = None) -> str:
    """The live envelope for a chat: answered inline, without the sender's working directory.

    The chat may belong to someone else, so the sender's local path stays on this machine.
    """
    from dataclasses import replace
    from .live.model import envelope

    return envelope(text, None if sender is None else replace(sender, cwd=""), origin,
                    "none" if reply == "none" else "inline", message_id, topic)


def deliver(query: str, wrapped: str, accounts: Callable[[], Iterable[dict]] | None = None) -> dict:
    """Post WRAPPED into the chat QUERY names and wait for Claude's reply."""
    from .cloud_cli import browser_provider
    from .providers.base import ProviderError

    row = saved_conversation(query, "claude-chat")
    discover = accounts or _accounts
    if discover is None:
        raise CloudSendError("Claude accounts are unavailable in this process; use the one-conv command.")
    try:
        provider = browser_provider("claude-chat", row["account_id"], discover)
    except click.ClickException as error:
        raise CloudSendError(error.message) from None
    try:
        sent = provider.send_message(row["id"], wrapped, timezone_name=local_timezone())
    except ProviderError as error:
        raise CloudSendError(f"{error}. Check the chat in the browser before resending.") from None
    return {"to": CLAUDE_CHAT + row["id"], "title": row["title"],
            "account": row["account_label"] or row["account_id"], "account_id": row["account_id"],
            "chat_message_id": sent.message_id, "reply_id": sent.reply_id,
            "stop_reason": sent.stop_reason, "reply": sent.reply,
            "url": f"https://claude.ai/chat/{row['id']}"}


def send_command(name: str, product: str) -> click.Command:
    """`one-conv cloud claude send`: `live send claude-chat:CONVERSATION`, with a chat-first dry run."""

    def invoke(conversation, message, from_stdin, sender, yes, as_json):
        from .live import router

        if from_stdin:
            message = sys.stdin.read()
        if not message or not message.strip():
            raise click.UsageError("Give MESSAGE or --stdin.")
        try:
            row = saved_conversation(conversation, product)
            me = router.whoami(sender)
            origin = "unknown" if me else router.origin(sys.stdin.isatty() and not from_stdin)
            if not yes:
                click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
                click.echo(f"  to:      {CLAUDE_CHAT}{row['id'][:8]}  {row['title']}  ({row['account_label'] or row['account_id']})")
                click.echo("  as:      the account itself; the chat replies to this turn")
                click.echo("  message:")
                text = cloud_envelope(message, me, origin)
                click.echo("\n".join("    " + line for line in text.splitlines()))
                return
            receipt = router.send(CLAUDE_CHAT + row["id"], message, me, origin)
        except (CloudSendError, router.LiveError) as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps(receipt, ensure_ascii=False))
        else:
            click.echo(f"sent: {receipt['title']} ({receipt['account']})", err=True)
            click.echo(receipt["reply"])
        click.echo(f"Run one-conv cloud {name} pull --account {receipt['account_id']} to save it in history.", err=True)

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
              "answers it, and the account's owner sees it in their history. Same route as "
              "`one-conv live send claude-chat:CONVERSATION`. Dry run without --yes."),
        epilog=(f"\b\nExamples:\n  one-conv cloud {name} send abcd1234 'What did we settle on?'\n"
                f"  one-conv cloud {name} send 'Trip planning' --stdin --yes < note.md"),
        context_settings={"help_option_names": ["-h", "--help"]})
