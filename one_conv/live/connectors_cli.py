"""`one-conv live connectors`: choose which CLIs this machine serves as remote MCP connectors."""

import json

import click

from . import connectors, hubclient

HELP = {"help_option_names": ["-h", "--help"]}


def add_connector_commands(live: click.Group) -> None:
    @live.group("connectors", context_settings=HELP, epilog="\b\nExamples:\n  one-conv live connectors ls\n"
                "  one-conv live connectors add deezer-cli email-cli\n  one-conv live connectors rm deezer-cli")
    def group():
        """Serve this machine's CLIs to claude.ai as custom connectors, through the hub.

        The bridge (`live serve`) publishes each chosen CLI and runs its tool calls here, with
        this machine's logins. Add a connector in claude.ai with the URL `ls` prints; the first
        use asks you to allow it in a browser paired with the workspace.
        """

    @group.command("ls", context_settings=HELP, epilog="\b\nExamples:\n  one-conv live connectors ls --json")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def list_command(as_json):
        """List the chosen CLIs and, from the hub, each connector's URL and whether its machine is online."""
        rows = {cli: {"cli": cli, "chosen": True} for cli in connectors.chosen()}
        try:
            hub = hubclient.configured()
            remote = hub.request("GET", "/v1/connectors").get("connectors", []) if hub else []
        except (hubclient.HubUnavailable, hubclient.HubRefused) as error:
            remote = []
            click.echo(f"hub unreachable: {error}", err=True)
        for row in remote:
            rows.setdefault(row["cli"], {"cli": row["cli"], "chosen": False}).update(row)
        if as_json:
            click.echo(json.dumps(list(rows.values())))
            return
        for row in rows.values():
            state = "live" if row.get("live") else "offline" if "live" in row else "not published yet"
            click.echo(f"{row['cli']:<20} {state:<18} {row.get('url', '')}")

    @group.command("add", context_settings=HELP, epilog="\b\nExamples:\n  one-conv live connectors add deezer-cli")
    @click.argument("clis", nargs=-1, required=True)
    def add_command(clis):
        """Serve CLIS (mcp-bridge names, see `mcp-bridge list`); the running bridge publishes them within a minute."""
        click.echo(" ".join(connectors.choose([*connectors.chosen(), *clis])))

    @group.command("rm", context_settings=HELP, epilog="\b\nExamples:\n  one-conv live connectors rm deezer-cli")
    @click.argument("clis", nargs=-1, required=True)
    def remove_command(clis):
        """Stop serving CLIS; claude.ai sees them disappear at the bridge's next publish."""
        click.echo(" ".join(connectors.choose([c for c in connectors.chosen() if c not in clis])))
