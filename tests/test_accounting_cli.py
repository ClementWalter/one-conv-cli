"""Public CLI imports and summaries use isolated registries and real optimized processes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from one_conv.hr_cli import build_hr_group


@pytest.fixture
def response():
    return {"id": "gen-fixture", "model": "fixture/model", "created": 1767226200,
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": "0.15"}}


@pytest.fixture
def source_file(tmp_path, response):
    source = tmp_path / "provider.jsonl"
    source.write_text(json.dumps(response) + "\n")
    return source


@pytest.fixture
def arguments(source_file):
    return ["ingest", str(source_file), "--source", "openrouter", "--cost-center", "jack",
            "--billing-scope", "fixture-account", "--worker", "fixture-worker", "--json"]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_ACCOUNTING_DIR", str(tmp_path / "ledger"))
    return build_hr_group()


@pytest.mark.parametrize("command", ["ingest", "usage", "summary"])
def test_help_has_examples(cli, command):
    assert "Examples" in CliRunner().invoke(cli, [command, "--help"]).output


def test_ingest_emits_recorded_count(cli, arguments):
    assert json.loads(CliRunner().invoke(cli, arguments).stdout)["recorded"] == 1


def test_repeated_ingest_is_duplicate(cli, arguments):
    CliRunner().invoke(cli, arguments)
    assert json.loads(CliRunner().invoke(cli, arguments).stdout)["duplicates"] == 1


def test_usage_lists_imported_model(cli, arguments):
    CliRunner().invoke(cli, arguments)
    assert json.loads(CliRunner().invoke(cli, ["usage", "--cost-center", "jack", "--json"]).stdout)[0]["model"] == "fixture/model"


def test_empty_summary_is_unknown(cli):
    assert json.loads(CliRunner().invoke(cli, ["summary", "--json"]).stdout)["observed_hourly_rate"]["amount"] is None


def test_provider_remains_unknown_in_cli(cli, arguments):
    CliRunner().invoke(cli, arguments)
    assert json.loads(CliRunner().invoke(cli, ["usage", "--json"]).stdout)[0]["provider"] is None


def test_stdin_import_is_supported(cli, response):
    result = CliRunner().invoke(cli, ["ingest", "-", "--source", "openrouter", "--cost-center", "jack",
                                    "--billing-scope", "fixture-account", "--worker", "fixture-worker", "--json"], input=json.dumps(response) + "\n")
    assert json.loads(result.stdout)["recorded"] == 1


def test_malformed_input_reports_error(cli, source_file, arguments):
    source_file.write_text("{broken}\n")
    assert "Invalid JSON usage record at line 1" in CliRunner().invoke(cli, arguments).output


def test_missing_worker_is_actionable(cli, arguments):
    index = arguments.index("--worker")
    del arguments[index:index + 2]
    assert "stable worker" in CliRunner().invoke(cli, arguments).output


def test_summary_rejects_inferred_occupied_time(cli):
    assert "require a cost center" in CliRunner().invoke(cli, ["summary", "--occupied-seconds", "60", "--json"]).output


@pytest.fixture
def public_cli(tmp_path):
    def run(*arguments):
        return subprocess.run([sys.executable, "-O", str(Path(__file__).parents[1] / "bin/one-conv"), "hr", *arguments],
                              env={**os.environ, "ONE_CONV_ACCOUNTING_DIR": str(tmp_path / "public-ledger")},
                              check=True, capture_output=True, text=True, timeout=15)
    return run


def test_public_cli_imports_and_reopens(public_cli, arguments):
    public_cli(*arguments)
    assert len(json.loads(public_cli("usage", "--cost-center", "jack", "--json").stdout)) == 1


def test_public_cli_deduplicates_reimports(public_cli, arguments):
    public_cli(*arguments)
    assert json.loads(public_cli(*arguments).stdout)["duplicates"] == 1


def test_public_cli_observed_rate(public_cli, arguments):
    public_cli(*arguments)
    result = public_cli("summary", "--cost-center", "jack", "--since", "2026-01-01T00:00:00Z",
                        "--until", "2026-01-01T01:00:00Z", "--occupied-seconds", "1800", "--json")
    assert json.loads(result.stdout)["observed_hourly_rate"]["amount"] == "0.30000000"


def test_public_cli_missing_cost_is_unknown(public_cli, arguments, source_file, response):
    response["usage"].pop("cost")
    source_file.write_text(json.dumps(response) + "\n")
    public_cli(*arguments)
    assert json.loads(public_cli("summary", "--cost-center", "jack", "--json").stdout)["cash_cost"]["unknown_events"] == 1


def test_explicit_directory_is_reusable(public_cli, arguments, tmp_path):
    directory = tmp_path / "server-owned"
    public_cli(*arguments, "--directory", str(directory))
    assert len(json.loads(public_cli("usage", "--directory", str(directory), "--json").stdout)) == 1
