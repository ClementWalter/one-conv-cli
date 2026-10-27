#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
"""Late provider evidence fills unknowns atomically without duplicating charges or rewriting facts."""

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from one_conv.accounting import AccountingConflict, Ledger
from one_conv.accounting.adapters import parse_events


@pytest.fixture
def event():
    return {"billing_scope": "router-account", "source": "openrouter", "source_event_id": "gen-example",
            "request_id": "gen-example", "cost_center_id": "jack", "worker_id": "node/omp:worker",
            "node_id": "node", "timestamp": "2026-01-01T00:10:00Z", "provenance": "Initial response usage"}


@pytest.fixture
def reported(event):
    return {**event, "source_event_id": "gen-example-metadata", "provenance": "Provider generation metadata",
            "model": "fixture/model", "provider": "fixture-provider", "tokens": {"input": 100, "output": 10},
            "cash_cost": {"amount": "0.05", "currency": "USD", "source": "generation total_cost"}}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger")


def test_unknown_cash_can_become_known_once(ledger, event, reported):
    ledger.record(event)
    ledger.record(reported)
    assert ledger.summary("jack")["cash_cost"]["reported"] == [{"currency": "USD", "amount": "0.05"}]


def test_enrichment_keeps_one_event(ledger, event, reported):
    ledger.record_many([event, reported])
    assert len(ledger.list_events()) == 1


def test_enrichment_marks_existing_identity(ledger, event, reported):
    ledger.record(event)
    receipt = ledger.record(reported)
    assert (receipt["status"], receipt["enriched"]) == ("duplicate", True)


def test_enrichment_retry_is_idempotent(ledger, event, reported):
    ledger.record_many([event, reported])
    assert ledger.record(reported)["enriched"] is False


def test_unknown_observation_cannot_erase_cash(ledger, event, reported):
    ledger.record_many([reported, event])
    assert ledger.list_events()[0]["cash_cost"]["amount"] == "0.05"


def test_new_cash_keeps_its_source(ledger, event, reported):
    ledger.record_many([event, reported])
    assert ledger.list_events()[0]["field_provenance"]["cash_cost"]["provenance"] == "Provider generation metadata"


def test_old_known_model_keeps_its_source(ledger, event, reported):
    event["model"] = "fixture/model"
    ledger.record_many([event, reported])
    assert ledger.list_events()[0]["field_provenance"]["model"]["provenance"] == "Initial response usage"


def test_known_cost_cannot_change(ledger, reported):
    ledger.record(reported)
    changed = copy.deepcopy(reported)
    changed["cash_cost"]["amount"] = "0.06"
    with pytest.raises(AccountingConflict):
        ledger.record(changed)


@pytest.mark.parametrize(("field", "value"), [("cost_center_id", "john"), ("worker_id", "node/omp:other"),
                                               ("node_id", "other-node"), ("timestamp", "2026-01-01T00:11:00Z")])
def test_enrichment_cannot_change_attribution(ledger, event, reported, field, value):
    ledger.record(event)
    with pytest.raises(AccountingConflict):
        ledger.record({**reported, field: value})


def test_conflict_rolls_back_prior_enrichment_in_batch(ledger, event, reported):
    ledger.record(event)
    try:
        ledger.record_many([reported, {**reported, "provider": "different-known-provider"}])
    except AccountingConflict:
        pass
    assert ledger.list_events()[0]["cash_cost"]["amount"] is None


def test_concurrent_same_evidence_fills_once(ledger, event, reported):
    ledger.record(event)
    with ThreadPoolExecutor(max_workers=4) as workers:
        receipts = list(workers.map(lambda _: ledger.record(reported), range(8)))
    assert sum(receipt["enriched"] for receipt in receipts) == 1


def test_concurrent_independent_fields_are_preserved(ledger, event):
    ledger.record(event)
    observations = [{**event, "model": "fixture/model"}, {**event, "provider": "fixture-provider"}]
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(ledger.record, observations))
    result = ledger.list_events()[0]
    assert (result["model"], result["provider"]) == ("fixture/model", "fixture-provider")


def test_openrouter_completion_and_generation_evidence_join(ledger):
    context = {"cost_center_id": "jack", "billing_scope": "router-account", "worker_id": "node/omp:worker"}
    completion = {"id": "gen-example", "created": 1767226200, "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
    generation = {"data": {"id": "gen-example", "created_at": "2026-01-01T00:10:00.123456Z",
                            "native_tokens_prompt": 100, "native_tokens_completion": 10, "total_cost": "0.05"}}
    ledger.record_many(parse_events("openrouter", [completion], **context)["events"])
    ledger.record_many(parse_events("openrouter", [generation], **context)["events"])
    assert ledger.summary("jack")["cash_cost"]["reported"] == [{"currency": "USD", "amount": "0.05"}]


def test_openrouter_join_preserves_completion_total_tokens():
    context = {"cost_center_id": "jack", "billing_scope": "router-account", "worker_id": "node/omp:worker"}
    completion = {"id": "gen-example", "created": 1767226200, "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
    generation = {"data": {"id": "gen-example", "created_at": "2026-01-01T00:10:00.123456Z", "total_cost": "0.05"}}
    assert parse_events("openrouter", [completion, generation], **context)["events"][0]["tokens"]["total"] == 110


def test_openrouter_join_cannot_erase_cash():
    context = {"cost_center_id": "jack", "billing_scope": "router-account", "worker_id": "node/omp:worker"}
    reported = {"id": "gen-example", "created": 1767226200, "usage": {"cost": "0.05"}}
    unknown = {"id": "gen-example", "created": 1767226200, "usage": {}}
    assert parse_events("openrouter", [reported, unknown], **context)["events"][0]["cash_cost"]["amount"] == "0.05"


def test_openrouter_join_rejects_conflicting_cash():
    context = {"cost_center_id": "jack", "billing_scope": "router-account", "worker_id": "node/omp:worker"}
    reported = {"id": "gen-example", "created": 1767226200, "usage": {"cost": "0.05"}}
    changed = {"id": "gen-example", "created": 1767226200, "usage": {"cost": "0.06"}}
    with pytest.raises(AccountingConflict):
        parse_events("openrouter", [reported, changed], **context)
