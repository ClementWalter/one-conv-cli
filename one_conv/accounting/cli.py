"""Local accounting commands import explicit files without contacting model providers."""

import json
import os
from pathlib import Path

import click

from ..live.model import live_home
from .adapters import parse_events, read_rows
from .client import configured_client
from .ledger import Ledger
from .model import AccountingError


def _directory(value):
    return value or Path(os.environ.get("ONE_CONV_ACCOUNTING_DIR", os.environ.get("ONE_CONV_HR_DIR", live_home() / "hr")))


def install_usage_commands(group):
    """Mount usage alongside rate cards while keeping ledger helpers independent of Click."""
    directory_option = click.option("--directory", type=click.Path(path_type=Path),
                                    help="Ledger directory; defaults to ONE_CONV_ACCOUNTING_DIR or the HR directory.")
    mode_option = click.option("--hub/--local", "use_hub", default=None,
                               help="Use the configured shared authority or force local data; default follows hub configuration.")

    @group.command("ingest", epilog=("Examples:\n"
                   "  one-conv hr ingest transcript.jsonl --source codex --cost-center jack --billing-scope codex-account --json\n"
                   "  one-conv hr ingest response.jsonl --source openrouter --cost-center jack --billing-scope router-account --worker jack-session --json"))
    @click.argument("input_file", type=click.File("r", encoding="utf-8"))
    @click.option("--source", required=True, type=click.Choice(["codex", "claude", "omp", "openrouter", "event"]))
    @click.option("--cost-center", required=True, help="Stable owning cost center for this worker and its internal subagents.")
    @click.option("--billing-scope", required=True, help="Stable billing authority/account ID; never an API key.")
    @click.option("--worker", help="Explicit worker reference; otherwise use the transcript's session ID.")
    @click.option("--parent-worker", help="Parent worker reference when importing an internal subagent.")
    @directory_option
    @mode_option
    @click.option("--json", "as_json", is_flag=True, help="Emit import counts and coverage as JSON.")
    def ingest(input_file, source, cost_center, billing_scope, worker, parent_worker, directory, as_json, use_hub):
        """Import bounded metadata-only JSONL/SSE. Repeated imports do not charge twice.

        Use '-' for stdin. The event format imports canonical records unchanged;
        its owner and billing scope must match the explicit flags.
        """
        try:
            parsed = parse_events(source, read_rows(input_file), cost_center_id=cost_center,
                                  billing_scope=billing_scope, worker_id=worker, parent_worker_id=parent_worker)
            if any(event["cost_center_id"] != cost_center or event["billing_scope"] != billing_scope for event in parsed["events"]):
                raise AccountingError("Canonical events must match --cost-center and --billing-scope")
            client = configured_client(use_hub, directory)
            receipts = (client or Ledger(_directory(directory))).record_many(parsed["events"])
        except (AccountingError, OSError, UnicodeError) as error:
            raise click.ClickException(str(error)) from error
        report = {"recorded": sum(receipt["status"] == "recorded" for receipt in receipts),
                  "duplicates": sum(receipt["status"] == "duplicate" for receipt in receipts),
                  "coverage": parsed["coverage"]}
        click.echo(json.dumps(report) if as_json else f"Recorded {report['recorded']}; duplicates {report['duplicates']}. {report['coverage']['notice']}")

    def filters(command):
        command = click.option("--cost-center", help="Owning cost center; all its internal workers are included.")(command)
        command = click.option("--since", help="Inclusive timezone-aware ISO timestamp.")(command)
        command = click.option("--until", help="Exclusive timezone-aware ISO timestamp.")(command)
        return mode_option(directory_option(command))

    @group.command("usage", epilog="Examples:\n  one-conv hr usage --cost-center jack --json\n  one-conv hr usage --limit 20 --json")
    @filters
    @click.option("--limit", default=1000, show_default=True, type=click.IntRange(1, 10000))
    @click.option("--json", "as_json", is_flag=True, help="Emit canonical events, including unknown fields as null.")
    def usage(cost_center, since, until, directory, limit, as_json, use_hub):
        """List the latest recorded usage events; no native transcript text is retained."""
        try:
            client = configured_client(use_hub, directory)
            events = (client or Ledger(_directory(directory))).list_events(cost_center, since=since, until=until, limit=limit)
        except (AccountingError, OSError) as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps(events, indent=None if as_json else 2))

    @group.command("summary", epilog=("Examples:\n  one-conv hr summary --cost-center jack --json\n"
                   "  one-conv hr summary --cost-center jack --since 2026-01-01T00:00:00Z "
                   "--until 2026-01-01T01:00:00Z --occupied-seconds 1800 --json"))
    @filters
    @click.option("--occupied-seconds", help="Explicit occupied owner seconds inside the selected window; never sum parallel child times.")
    @click.option("--json", "as_json", is_flag=True, help="Emit separate cash, allocation, estimate, quota and coverage totals.")
    def summary(cost_center, since, until, directory, occupied_seconds, as_json, use_hub):
        """Summarize recorded events; reported cash hourly rate needs a complete, explicit window."""
        try:
            client = configured_client(use_hub, directory)
            result = (client or Ledger(_directory(directory))).summary(cost_center, since=since, until=until, occupied_seconds=occupied_seconds)
        except (AccountingError, OSError) as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps(result, indent=None if as_json else 2))
