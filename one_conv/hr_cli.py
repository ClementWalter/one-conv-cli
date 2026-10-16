"""Non-interactive HR commands expose reusable local rate-card and quote helpers."""

import json

import click

from .accounting.client import configured_client
from .accounting.model import AccountingError
from .hr import RateError, RateStore, make_card, quote

_mode_option = click.option("--hub/--local", "use_hub", default=None,
                            help="Use the configured shared authority or force local data; default follows hub configuration.")


def _card_text(card):
    rate = card["hourly_rate"]
    amount = "unknown" if rate["amount"] is None else f"{rate['amount']} {rate['currency']}/hour"
    return (f"{card['cost_center_id']}  {card['name']}  {amount} ({rate['basis']})\n"
            f"  References: {', '.join((*card['aliases'], *card['addresses'])) or 'none'}\n"
            f"  Provenance: {rate['provenance']}\n  Metering basis: {rate['metering_basis']}\n"
            "  Includes internal subagents")


def build_hr_group():
    """Register accounting inputs without choosing staffing or contacting agents."""
    @click.group("hr", context_settings={"help_option_names": ["-h", "--help"]},
                 epilog="Examples:\n  one-conv hr list --json\n  one-conv hr quote jack --minutes 15 --json")
    def group():
        """Manage hourly rates, import actual usage and estimate the cost of an ask.

        Rates include internal subagents. Cards are local, explicit planning inputs;
        they do not meter providers, synchronize machines or automate staffing.
        """

    @group.command("register", epilog=("Examples (illustrative rates):\n"
                   "  one-conv hr register jack --name Jack --alias jack --hourly-rate 12 --currency USD "
                   "--basis quoted --provenance 'Example budget rate' --metering-basis 'Occupied work hour'\n"
                   "  one-conv hr register john --name John --unknown-rate --currency USD "
                   "--basis quoted --provenance 'Not measured' --metering-basis 'Occupied work hour'"))
    @click.argument("cost_center_id")
    @click.option("--name", required=True, help="Display name; the cost center ID stays stable on rename.")
    @click.option("--alias", "aliases", multiple=True, help="Name reference, such as jack or @jack; repeatable.")
    @click.option("--address", "addresses", multiple=True, help="Exact agent address reference; repeatable, not verified live.")
    @click.option("--hourly-rate", help="All-in amount per hour, 0-1000000000, at most 8 decimal places.")
    @click.option("--unknown-rate", is_flag=True, help="Record an unknown hourly rate; distinct from zero.")
    @click.option("--currency", required=True, help="Three-letter currency code, such as USD. No conversion occurs.")
    @click.option("--basis", required=True, type=click.Choice(["observed", "allocated", "quoted"]))
    @click.option("--provenance", required=True, help="Source or explanation of the rate, including unknowns.")
    @click.option("--metering-basis", required=True, help="What an hour means, such as occupied work time including tools.")
    @click.option("--replace", is_flag=True, help="Replace the whole card for an existing stable ID.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the stored card as JSON.")
    @_mode_option
    def register(cost_center_id, name, aliases, addresses, hourly_rate, unknown_rate,
                 currency, basis, provenance, metering_basis, replace, as_json, use_hub):
        """Create a card; identical retries are safe. Changed cards need --replace."""
        if (hourly_rate is not None) == unknown_rate:
            raise click.UsageError("Choose exactly one of --hourly-rate AMOUNT or --unknown-rate. "
                                   "Example: one-conv hr register --help")
        try:
            card = make_card(cost_center_id, name, aliases=aliases, addresses=addresses,
                             hourly_rate=hourly_rate, currency=currency, basis=basis,
                             provenance=provenance, metering_basis=metering_basis)
            client = configured_client(use_hub)
            saved = client.register_rate(card, replace=replace) if client else RateStore().register(card, replace=replace)
        except (RateError, AccountingError, OSError) as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps(saved) if as_json else _card_text(saved))

    @group.command("list", epilog="Examples:\n  one-conv hr list\n  one-conv hr list --json")
    @click.option("--json", "as_json", is_flag=True, help="Emit an array of rate cards.")
    @_mode_option
    def list_cards(as_json, use_hub):
        """List shared cards when a hub is configured, or local cards with --local."""
        try:
            client = configured_client(use_hub)
            cards = client.rates() if client else RateStore().list()
        except (RateError, AccountingError, OSError) as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps(cards) if as_json else "\n".join(map(_card_text, cards)) or "No rate cards. Run one-conv hr register --help.")

    @group.command("show", epilog="Examples:\n  one-conv hr show jack\n  one-conv hr show @jack --json")
    @click.argument("reference")
    @click.option("--json", "as_json", is_flag=True, help="Emit the complete rate card.")
    @_mode_option
    def show(reference, as_json, use_hub):
        """Look up an exact stable ID, @alias or stored agent address."""
        try:
            client = configured_client(use_hub)
            card = client.show_rate(reference) if client else RateStore().show(reference)
        except (RateError, AccountingError, OSError) as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps(card) if as_json else _card_text(card))

    @group.command("quote", epilog="Examples:\n  one-conv hr quote jack --minutes 15\n  one-conv hr quote @jack --minutes 2.5 --json")
    @click.argument("reference")
    @click.option("--minutes", required=True, help="Estimated occupied work minutes, 0-525600; at most 8 decimal places.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the estimate and complete rate metadata.")
    @_mode_option
    def quote_ask(reference, minutes, as_json, use_hub):
        """Estimate rate * minutes / 60; this does not send the ask or incur a charge."""
        try:
            client = configured_client(use_hub)
            estimate = client.quote(reference, minutes) if client else quote(RateStore().show(reference), minutes)
        except (RateError, AccountingError, OSError) as error:
            raise click.ClickException(str(error)) from error
        if as_json:
            click.echo(json.dumps(estimate))
        else:
            cost = estimate["expected_cost"]
            amount = "unknown" if cost["amount"] is None else f"{cost['amount']} {cost['currency']}"
            click.echo(f"{estimate['name']} [{estimate['cost_center_id']}]: {amount} for {estimate['estimated_minutes']} estimated minutes\n"
                       f"Basis: {estimate['hourly_rate']['basis']}\n"
                       f"Provenance: {estimate['hourly_rate']['provenance']}\n"
                       f"Metering basis: {estimate['hourly_rate']['metering_basis']}\n"
                       f"Includes internal subagents. {estimate['notice']}")

    from .accounting.cli import install_usage_commands

    install_usage_commands(group)
    return group
