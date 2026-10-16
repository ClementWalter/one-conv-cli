"""Shared accounting and managed staffing preserve browser and node authority boundaries."""

import concurrent.futures
import sqlite3
import stat

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from one_conv.control.cli import build_control_group
from one_conv.control.runtime import Workers, application
from one_conv.hr import make_card
from one_conv.live.cli import build_live_group

ORIGIN = "https://workspace.example.test"


@pytest.fixture
def app(tmp_path):
    return application(tmp_path, ORIGIN)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as active:
        yield active


@pytest.fixture
def node(client, app):
    token = app.state.hub.add_token("worker")
    client.headers["Authorization"] = "Bearer " + token
    return client


@pytest.fixture
def event():
    return {"billing_scope": "subscription-account", "source": "codex", "source_event_id": "answer-1",
            "cost_center_id": "jack", "worker_id": "worker/codex:session", "timestamp": "2026-10-16T12:00:00Z",
            "provenance": "native token record", "tokens": {"input": 100, "output": 20}}


@pytest.fixture
def recorded(node, event):
    return node.post("/v1/accounting/events", json={"events": [event]})


def test_accounting_api_requires_a_bearer(client):
    assert client.get("/v1/accounting/usage").status_code == 401


def test_browser_cookie_cannot_report_agent_usage(client, app, event):
    client.post("/api/login", json={"token": app.state.auth.issue_pairing_token()}, headers={"Origin": ORIGIN})
    assert client.post("/v1/accounting/events", json={"events": [event]}).status_code == 401


def test_usage_identity_is_derived_from_the_bearer(recorded):
    assert recorded.json()["receipts"][0]["event"]["node_id"] == "worker"


def test_subscription_cost_remains_unknown(recorded):
    assert recorded.json()["receipts"][0]["event"]["cash_cost"]["amount"] is None


def test_accounting_retry_deduplicates_in_the_shared_ledger(node, event, recorded):
    assert node.post("/v1/accounting/events", json={"events": [event]}).json()["duplicates"] == 1


@pytest.mark.parametrize("change", [{"node_id": "another"}, {"worker_id": "another/codex:session"},
                                   {"parent_worker_id": "another/codex:parent"}])
def test_node_cannot_report_another_nodes_worker(node, event, change):
    assert node.post("/v1/accounting/events", json={"events": [{**event, **change}]}).status_code == 403


def test_rejected_batch_does_not_leave_partial_costs(node, event, app):
    node.post("/v1/accounting/events", json={"events": [event, {**event, "source_event_id": "another", "worker_id": "foreign/omp:1"}]})
    assert app.state.accounting.ledger.list_events() == []


def test_shared_usage_endpoint_returns_imported_events(node, recorded):
    assert len(node.get("/v1/accounting/usage").json()["events"]) == 1


def test_shared_summary_reports_unknown_cash(node, recorded):
    assert node.get("/v1/accounting/summary").json()["summary"]["cash_cost"]["unknown_events"] == 1


@pytest.mark.parametrize("addresses", [["another/codex:session"], ["another/codex:session", "codex:legacy"]])
def test_usage_cannot_charge_another_nodes_known_cost_center(node, app, card, event, addresses):
    app.state.accounting.rates.register({**card, "addresses": addresses})
    assert node.post("/v1/accounting/events", json={"events": [event]}).status_code == 403


@pytest.mark.parametrize("addresses", [[], ["codex:legacy"], ["another/codex:session", "worker/codex:session"]])
def test_unknown_or_shared_cost_center_ownership_allows_usage(node, app, card, event, addresses):
    app.state.accounting.rates.register({**card, "addresses": addresses})
    assert node.post("/v1/accounting/events", json={"events": [event]}).status_code == 200


def test_known_owner_allows_distinct_native_child_usage(node, app, card, event):
    app.state.accounting.rates.register(card)
    child = {**event, "worker_id": "worker/codex:child", "parent_worker_id": "worker/codex:session"}
    assert node.post("/v1/accounting/events", json={"events": [child]}).status_code == 200


def test_wrong_owner_rejects_the_whole_usage_batch(node, app, card, event):
    app.state.accounting.rates.register({**card, "addresses": ["another/codex:session"]})
    node.post("/v1/accounting/events", json={"events": [{**event, "source_event_id": "unowned", "cost_center_id": "john"}, event]})
    assert app.state.accounting.ledger.list_events() == []


def test_rate_ownership_stays_locked_through_the_ledger_write(node, app, event, monkeypatch):
    ledger = app.state.accounting.ledger
    original = ledger.record_many
    ownership_locked = []

    def record(events):
        with sqlite3.connect(app.state.accounting.rates.path, timeout=0) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                ownership_locked.append(True)
        return original(events)

    monkeypatch.setattr(ledger, "record_many", record)
    node.post("/v1/accounting/events", json={"events": [event]})
    assert ownership_locked == [True]


def test_node_bearer_does_not_authorize_browser_accounting(node):
    assert node.get("/api/providers/accounting").status_code == 401


@pytest.fixture
def card():
    return make_card("jack", "Jack", addresses=["worker/codex:session"], currency="USD", basis="allocated",
                     provenance="Subscription allocation", metering_basis="Occupied work hour")


@pytest.fixture
def paired_browser(client, app):
    client.post("/api/login", json={"token": app.state.auth.issue_pairing_token()}, headers={"Origin": ORIGIN})
    client.headers.update({"Origin": ORIGIN, "X-One-Conv-Token": client.get("/api/session").json()["csrf_token"]})
    return client


@pytest.fixture
def planning_update(paired_browser, app, card):
    cached = app.state.accounting.rates.register({**card, "addresses": []})
    Workers(None, app.state.accounting.rates).rate_card({"spec": {"name": "jack"}, "address": "worker/codex:session"})
    return paired_browser.post("/api/providers/accounting", json={"action": "update_rate", "reference": cached["cost_center_id"],
                                                               "hourly_rate": {**cached["hourly_rate"], "amount": "5"}})


def test_browser_partial_rate_update_preserves_ready_worker_address(planning_update):
    assert planning_update.json()["card"]["addresses"] == ["worker/codex:session"]


def test_browser_partial_rate_update_sets_the_planning_rate(planning_update):
    assert planning_update.json()["card"]["hourly_rate"]["amount"] == "5"


def test_worker_reconciliation_preserves_the_latest_browser_rate(planning_update, app):
    Workers(None, app.state.accounting.rates).rate_card({"spec": {"name": "jack"}, "address": "worker/codex:second"})
    assert app.state.accounting.rates.show("jack")["hourly_rate"]["amount"] == "5"


def test_browser_rate_update_requires_browser_authority(node, card):
    assert node.post("/api/providers/accounting", json={"action": "update_rate", "reference": "jack", "hourly_rate": card["hourly_rate"]}).status_code == 401


def test_browser_rate_update_cannot_submit_stale_identity_fields(paired_browser, card):
    assert paired_browser.post("/api/providers/accounting", json={"action": "update_rate", "reference": "jack", "hourly_rate": card["hourly_rate"], "addresses": []}).status_code == 400


def test_node_can_publish_its_rate_card(node, card):
    assert node.post("/v1/accounting/rates", json={"card": card}).status_code == 200


def test_quote_of_an_unknown_rate_stays_unknown(node, card):
    node.post("/v1/accounting/rates", json={"card": card})
    assert node.post("/v1/accounting/quote", json={"reference": "jack", "minutes": "15"}).json()["quote"]["expected_cost"]["status"] == "unknown"


def test_a_node_cannot_take_over_a_human_managed_card(node, card, app):
    app.state.accounting.rates.register({**card, "addresses": []})
    assert node.post("/v1/accounting/rates", json={"card": card, "replace": True}).status_code == 403


def test_competing_nodes_cannot_replace_the_same_new_owner(app, card):
    from one_conv.live.hub import HubError

    def register(node):
        try:
            app.state.accounting.node(node, "rates", "POST", {"card": {**card, "addresses": [node + "/codex:session"]},
                                                              "replace": True})
            return "saved"
        except HubError:
            return "refused"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(register, ["first", "second"]))
    assert sorted(outcomes) == ["refused", "saved"]


def test_cloud_staffing_is_not_enabled_by_a_request(node):
    assert node.get("/v1/workers").status_code == 404


@pytest.mark.parametrize("identifier", [[], {}, None, 1, "../escape", ""])
def test_worker_actions_refuse_malformed_ids_before_dispatch(identifier):
    with pytest.raises(ValueError, match="tracked worker"):
        Workers(None, None).browser("POST", {"action": "stop", "id": identifier})


def test_reconciliation_refuses_a_malformed_id_before_remote_inspection():
    with pytest.raises(ValueError, match="tracked worker"):
        Workers(None, None).browser("POST", {"action": "reconcile", "id": [], "box_id": "bx_example"})


def test_pairing_cli_writes_a_private_link(tmp_path):
    path = tmp_path / "phone.link"
    CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path / "state"), "pair", "--origin", ORIGIN,
                                               "--output", str(path)])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pairing_cli_does_not_echo_the_secret(tmp_path):
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path / "state"), "pair", "--origin", ORIGIN,
                                                       "--output", str(tmp_path / "phone.link")])
    assert "#pair=" not in result.output


def test_node_cli_refuses_the_human_mailbox_identity(tmp_path):
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path), "node", "add", "workspace",
                                                       "--output", str(tmp_path / "key")])
    assert result.exit_code == 1


@pytest.fixture
def cloud_spawn(monkeypatch):
    from one_conv.control import client
    calls = []

    def submit(spec, ident):
        calls.append({"spec": spec, "id": ident})
        return {"id": ident, "status": "queued", "spec": spec}

    monkeypatch.setattr(client, "submit", submit)
    result = CliRunner().invoke(build_live_group([]), ["spawn", "codex", "Review the change.", "--cloud",
                                                      "--name", "jack", "--topic", "release", "--yes", "--json"])
    return calls, result


def test_cloud_spawn_does_not_start_a_local_process(cloud_spawn):
    assert len(cloud_spawn[0]) == 1


def test_cloud_spawn_preserves_the_requested_task(cloud_spawn):
    assert cloud_spawn[0][0]["spec"]["prompt"] == "Review the change."


def test_cloud_spawn_returns_its_durable_request_id(cloud_spawn):
    assert cloud_spawn[0][0]["id"] in cloud_spawn[1].output
