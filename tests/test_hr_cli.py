"""CLI coverage exercises explicit registration, reusable estimates and real process entry."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from one_conv.hr_cli import build_hr_group


@pytest.fixture
def registration():
    return ["register", "jack", "--name", "Jack", "--alias", "jack", "--address", "codex:example-session",
            "--hourly-rate", "12", "--currency", "USD", "--basis", "allocated",
            "--provenance", "Example allocation", "--metering-basis", "Occupied work hour", "--json"]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_HR_DIR", str(tmp_path / "hr"))
    return build_hr_group()


@pytest.mark.parametrize("command", [[], ["register"], ["list"], ["show"], ["quote"]])
def test_command_help_includes_examples(cli, command):
    assert "Examples" in CliRunner().invoke(cli, [*command, "--help"]).output


def test_empty_list_emits_json(cli):
    assert json.loads(CliRunner().invoke(cli, ["list", "--json"]).stdout) == []


def test_registration_returns_stable_id(cli, registration):
    assert json.loads(CliRunner().invoke(cli, registration).stdout)["cost_center_id"] == "jack"


def test_unknown_registration_needs_explicit_flag(cli, registration):
    del registration[registration.index("--hourly-rate"):registration.index("--hourly-rate") + 2]
    assert "Choose exactly one" in CliRunner().invoke(cli, registration).output


def test_conflicting_rate_flags_are_rejected(cli, registration):
    assert CliRunner().invoke(cli, [*registration, "--unknown-rate"]).exit_code == 2


def test_unknown_rate_is_json_null(cli, registration):
    index = registration.index("--hourly-rate")
    registration[index:index + 2] = ["--unknown-rate"]
    assert json.loads(CliRunner().invoke(cli, registration).stdout)["hourly_rate"]["amount"] is None


def test_invalid_rate_has_actionable_error(cli, registration):
    registration[registration.index("--hourly-rate") + 1] = "NaN"
    assert "finite" in CliRunner().invoke(cli, registration).output


def test_changed_card_requires_replace(cli, registration):
    CliRunner().invoke(cli, registration)
    registration[registration.index("--name") + 1] = "Renamed Jack"
    assert "--replace" in CliRunner().invoke(cli, registration).output


def test_human_quote_explains_allocation(cli, registration):
    CliRunner().invoke(cli, registration)
    assert "Basis: allocated" in CliRunner().invoke(cli, ["quote", "jack", "--minutes", "15"]).output


@pytest.fixture
def public_cli(tmp_path):
    def run(*arguments):
        return subprocess.run([sys.executable, "-O", str(Path(__file__).parents[1] / "bin/one-conv"), "hr", *arguments],
                              env={**os.environ, "ONE_CONV_HR_DIR": str(tmp_path / "public-hr")},
                              capture_output=True, text=True, check=True, timeout=15)
    return run


def test_public_process_registers_and_reads_card(public_cli, registration):
    saved = json.loads(public_cli(*registration).stdout)
    assert json.loads(public_cli("show", "@jack", "--json").stdout) == saved


def test_public_process_quotes_address_reference(public_cli, registration):
    public_cli(*registration)
    result = public_cli("quote", "codex:example-session", "--minutes", "15", "--json")
    assert json.loads(result.stdout)["expected_cost"]["amount"] == "3.00000000"


def test_public_process_quote_carries_provenance(public_cli, registration):
    public_cli(*registration)
    result = public_cli("quote", "jack", "--minutes", "15", "--json")
    assert json.loads(result.stdout)["hourly_rate"]["provenance"] == "Example allocation"


def test_public_process_lists_persisted_card(public_cli, registration):
    public_cli(*registration)
    assert len(json.loads(public_cli("list", "--json").stdout)) == 1


def test_public_process_unknown_quote_is_not_free(public_cli, registration):
    index = registration.index("--hourly-rate")
    registration[index:index + 2] = ["--unknown-rate"]
    public_cli(*registration)
    result = public_cli("quote", "jack", "--minutes", "15", "--json")
    assert json.loads(result.stdout)["expected_cost"] == {"amount": None, "currency": "USD", "status": "unknown"}
