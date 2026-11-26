"""`one-conv live share`: give someone a link to chat with one agent session, list and revoke such links."""

import json
import time

import click

from . import router, share

HELP = {"help_option_names": ["-h", "--help"]}


def _when(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch or 0))


def add_share_command(live: click.Group) -> None:
    @live.command("share", context_settings=HELP,
                  epilog="\b\nExamples:\n"
                         "  one-conv live share --label Alice --yes             # inside a session: share it\n"
                         "  one-conv live share --ttl 2h --label 'support call' --yes --json\n"
                         "  one-conv live share --session laptop/claude:4f1a2b3c-0000-4000-8000-000000000001 --yes\n"
                         "  one-conv live share --list\n"
                         "  one-conv live share --revoke 3f9a0c1d2e4b5a6f --yes\n"
                         "  one-conv live send guest:3f9a0c1d2e4b5a6f 'happy to help' --yes   # answer the guest yourself")
    @click.option("--label", default=share.DEFAULT_LABEL, show_default=True,
                  help="Who the link is for; the agent sees it as the guest's name.")
    @click.option("--ttl", default=share.DEFAULT_TTL, show_default=True,
                  help="How long the link works: 90m, 24h, 7d (at most 30d).")
    @click.option("--session", "address", metavar="ADDRESS",
                  help="The session to share (harness:id or NODE/harness:id); defaults to the calling agent.")
    @click.option("--list", "show_list", is_flag=True, help="List this machine's shares.")
    @click.option("--revoke", metavar="ID", help="End a share at once.")
    @click.option("--yes", is_flag=True, help="Create (or revoke). Without it, only shows what would happen.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def share_command(label, ttl, address, show_list, revoke, yes, as_json):
        """Let a colleague's agent (or a person) talk to ONE agent session: this one, or --session.

        Prints, each carrying the share's secret: a `claude mcp add` command and a Codex
        config.toml block for the share's MCP server (`ask`, `send`, `read_replies`,
        `session_info`), a `one-conv live send '<link>'` command, and the link to a chat page
        for a person. `--json` gives them as separate fields. A guest sees only their own
        exchange with the session, never its transcript. Their messages arrive marked kind="guest": the agent is told they are not you, grant
        no approval and must not reveal secrets, files or other conversations unless your own
        instructions in the session allow it. It answers with send_message to guest:<id>.
        The session must be reachable through the relay (this machine's bridge running); a
        guest's message while it is offline is refused, not queued. The link carries its
        secret after `#`, so treat it like a password; you get an inbox note (tag `share`)
        when it is made and when its guest first writes.
        """
        try:
            if show_list:
                _list(as_json)
            elif revoke:
                _revoke(revoke, yes, as_json)
            else:
                _create(label, ttl, address, yes, as_json)
        except (share.ShareError, router.LiveError) as error:
            raise click.ClickException(str(error)) from None

    def _create(label, ttl, address, yes, as_json):
        seconds = share.parse_ttl(ttl)
        target = share.session_address(None if address else router.whoami(), address)
        if not yes:
            click.echo("DRY-RUN (nothing shared). Re-run with --yes to make the link.")
            click.echo(f"  session:  {target}")
            click.echo(f"  guest:    {label}, for {ttl}")
            return
        made = share.create(target, label, seconds)
        if as_json:
            click.echo(json.dumps(made))
            return
        mark = share.SECRET_MARK
        click.echo(f"Share {made['id']}: {made['label']} can reach {made['address']} until {_when(made['expires'])}.\n"
                   f"Lines marked {mark} carry the share's secret: pass them only to {made['label']}, like a password.\n")
        click.echo(f"For their agent, Claude Code {mark}:\n  {made['claude_command']}\n")
        click.echo(f"For their agent, Codex, in ~/.codex/config.toml {mark}:")
        click.echo("".join(f"  {line}\n" for line in made["codex_config"].splitlines()))
        click.echo(f"For their one-conv {mark} (prints the answer):\n  {made['one_conv_command']}\n")
        click.echo(f"For a person, the chat page {mark}:\n  {made['link']}")
        if made.get("warning"):
            click.echo(f"warning: {made['warning']}", err=True)

    def _list(as_json):
        rows = share.listing()
        if as_json:
            click.echo(json.dumps(rows))
            return
        if not rows:
            click.echo("No shares made from this machine.", err=True)
        for row in rows:
            until = _when(row["expires"])
            click.echo(f"{row['id']}  {row['state']:<8} {row['label'][:20]:<20} {row['address']}  until {until}"
                       f"  {row['guest_messages']} guest message(s)")

    def _revoke(ident, yes, as_json):
        if not yes:
            click.echo(f"DRY-RUN: would revoke share {ident}; its link stops working at once. Re-run with --yes.")
            return
        result = share.revoke(ident)
        click.echo(json.dumps(result) if as_json else f"revoked share {ident}")
