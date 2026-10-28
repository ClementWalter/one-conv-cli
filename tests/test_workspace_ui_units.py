#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
#     "mcp==2.2.0",
#     "websockets==17.2",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
#     "httpx==0.28.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Pure workspace identity, route, and timestamp helpers run without browser networking."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def evaluate_ui():
    """Load the production declarations with inert browser globals before UI event binding."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for workspace helper tests")
    source = (Path(__file__).parents[1] / "one_conv/live/web_assets/app.js").read_text()
    declarations = source.split('$("composer").addEventListener("submit", send);', 1)[0]

    def evaluate(expression, setup=""):
        globals_source = """
const location = {hash: '', pathname: '/', search: ''};
const history = {replaceState() {}};
const localStorage = {getItem() {return null;}, setItem() {}};
const document = {};
const performance = {now: () => 0};
Date.now = () => 1700000000000;
"""
        result = subprocess.run([node, "-e", globals_source + declarations + setup +
                                 "\nprocess.stdout.write(JSON.stringify(" + expression + "));"],
                                text=True, capture_output=True, check=True, timeout=5)
        return json.loads(result.stdout)

    return evaluate


@pytest.mark.parametrize(("age", "expected"), [(59, "busy"), (60, "busy"), (61, "offline")])
def test_report_freshness_uses_the_server_lease(evaluate_ui, age, expected):
    assert evaluate_ui("presenceOf({session:{presence:{state:'busy',last_seen:1700000000-" + str(age) +
                       ",bridge_last_seen:1700000000}}}).state") == expected


def test_fresh_bridge_cannot_keep_a_stale_session_online(evaluate_ui):
    assert evaluate_ui("presenceOf({session:{presence:{state:'idle',last_seen:1699999900,bridge_last_seen:1700000000}}}).can_message") is False


def test_fresh_session_cannot_keep_a_stale_bridge_online(evaluate_ui):
    assert evaluate_ui("presenceOf({session:{presence:{state:'idle',last_seen:1700000000,bridge_last_seen:1699999900}}}).state") == "offline"


def test_loaded_legacy_entry_is_unverified(evaluate_ui):
    assert evaluate_ui("presenceOf({session:{status:'loaded',online:true}}).state") == "unverified"


@pytest.mark.parametrize("server_offset", [-90, 5, 90])
def test_report_clock_tolerates_device_wall_clock_skew(evaluate_ui, server_offset):
    setup = "model.state.at=1700000000+" + str(server_offset) + ";"
    assert evaluate_ui("presenceOf({session:{presence:{state:'idle',last_seen:model.state.at,bridge_last_seen:model.state.at,can_message:true}}}).state", setup) == "idle"


def test_monotonic_elapsed_time_expires_the_last_successful_report(evaluate_ui):
    setup = "model.state.at=1700000000;model.reportReceivedAt=0;performance.now=()=>61000;"
    assert evaluate_ui("presenceOf({session:{presence:{state:'idle',last_seen:1700000000,bridge_last_seen:1700000000}}}).state", setup) == "offline"


def test_device_clock_changes_do_not_age_a_fresh_report(evaluate_ui):
    setup = "model.state.at=1700000000;model.reportReceivedAt=0;performance.now=()=>1000;Date.now=()=>1800000000000;"
    assert evaluate_ui("presenceOf({session:{presence:{state:'idle',last_seen:1700000000,bridge_last_seen:1700000000}}}).state", setup) == "idle"


def test_unnamed_identity_uses_harness_and_short_native_id(evaluate_ui):
    assert evaluate_ui("person('node/codex:123456789abcdef').name") == "Codex · 12345678"


def test_native_title_is_context_rather_than_identity(evaluate_ui):
    assert evaluate_ui("person('node/codex:123456789abcdef').name",
                       "model.state.sessions=[{address:'node/codex:123456789abcdef',title:'Unstable title'}];") == "Codex · 12345678"


def test_named_identity_has_a_single_at_prefix(evaluate_ui):
    assert evaluate_ui("person('node/codex:123').name", "model.state.names={'@jack':'node/codex:123'};") == "@jack"


def test_explicit_route_wins_over_saved_route(evaluate_ui):
    assert evaluate_ui("requestedView()", "navigation.view={kind:'topic',key:'zulu'};location.hash='#topic=alpha';") == {"kind": "topic", "key": "alpha"}


def test_missing_topic_is_still_a_valid_stable_route(evaluate_ui):
    assert evaluate_ui("requestedView()", "navigation.view={kind:'topic',key:'missing'};") == {"kind": "topic", "key": "missing"}


def test_saved_route_rejects_non_string_identity(evaluate_ui):
    assert evaluate_ui("validView({kind:'direct',key:{}})") is False
