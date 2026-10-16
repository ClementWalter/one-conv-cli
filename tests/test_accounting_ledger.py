"""Ledger tests cover immutable identity, ownership rollups, money bases and concurrent writers."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from one_conv.accounting import (
    AccountingConflict,
    AccountingError,
    Ledger,
    normalize_event,
)


@pytest.fixture
def event():
    return {"billing_scope": "router-account", "source": "openrouter", "source_event_id": "gen-1",
            "request_id": "gen-1", "cost_center_id": "jack", "worker_id": "worker-child",
            "parent_worker_id": "worker-parent", "timestamp": "2026-01-01T00:10:00Z",
            "provenance": "Sanitized provider usage fixture", "model": "example/model", "provider": "example-provider",
            "tokens": {"input": 100, "output": 10, "cached_input": 80, "reasoning": 4},
            "cash_cost": {"amount": "0.15", "currency": "USD", "source": "usage.cost"}}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger #? é")


def test_record_returns_stable_identity(ledger, event):
    assert ledger.record(event)["event"]["event_id"] == normalize_event(event)["event_id"]


def test_retry_is_duplicate(ledger, event):
    ledger.record(event)
    assert ledger.record(event)["status"] == "duplicate"


def test_same_request_cannot_charge_twice(ledger, event):
    ledger.record_many([event, event])
    assert len(ledger.list_events()) == 1


def test_copied_observation_deduplicates_provider_request(ledger, event):
    ledger.record(event)
    assert ledger.record({**event, "source": "import-copy", "source_event_id": "copied-row", "provenance": "Copied observation"})["status"] == "duplicate"


def test_same_request_in_different_accounts_is_distinct(ledger, event):
    ledger.record_many([event, {**event, "billing_scope": "another-account"}])
    assert len(ledger.list_events()) == 2


def test_owner_conflict_is_visible(ledger, event):
    ledger.record(event)
    with pytest.raises(AccountingConflict):
        ledger.record({**event, "cost_center_id": "john"})


def test_cost_conflict_rolls_back_entire_batch(ledger, event):
    ledger.record(event)
    changed = copy.deepcopy(event)
    changed["cash_cost"]["amount"] = "0.30"
    try:
        ledger.record_many([{**event, "request_id": "gen-2"}, changed])
    except AccountingConflict:
        pass
    assert len(ledger.list_events()) == 1


def test_equal_decimal_representations_are_idempotent(ledger, event):
    ledger.record(event)
    event["cash_cost"]["amount"] = "0.150"
    assert ledger.record(event)["status"] == "duplicate"


def test_same_charge_with_another_source_reference_is_duplicate(ledger, event):
    ledger.record(event)
    event["cash_cost"]["source"] = "generation total_cost"
    assert ledger.record(event)["status"] == "duplicate"


def test_invalid_quota_shape_is_not_unknown(event):
    event["quota_usage"] = []
    with pytest.raises(AccountingError):
        normalize_event(event)


def test_summary_does_not_truncate_at_listing_limit(ledger, event):
    ledger.record_many({**event, "request_id": f"gen-{index}"} for index in range(1001))
    assert ledger.summary("jack")["event_count"] == 1001


def test_unknown_cash_blocks_observed_rate(ledger, event):
    event.pop("cash_cost")
    ledger.record(event)
    result = ledger.summary("jack", since="2026-01-01T00:00:00Z", until="2026-01-01T01:00:00Z", occupied_seconds="1800")
    assert result["observed_hourly_rate"]["amount"] is None


def test_internal_subagent_is_charged_to_owner(ledger, event):
    ledger.record(event)
    assert ledger.summary("jack")["event_count"] == 1


def test_worker_is_not_an_additional_cost_center(ledger, event):
    ledger.record(event)
    assert ledger.summary("worker-child")["event_count"] == 0


def test_missing_cash_is_unknown(ledger, event):
    event.pop("cash_cost")
    ledger.record(event)
    assert ledger.summary("jack")["cash_cost"] == {"reported": [], "unknown_events": 1, "complete": False}


def test_reported_zero_is_known(ledger, event):
    event["cash_cost"]["amount"] = "0"
    ledger.record(event)
    assert ledger.summary("jack")["cash_cost"] == {"reported": [{"currency": "USD", "amount": "0"}], "unknown_events": 0, "complete": True}


def test_allocation_never_becomes_cash(ledger, event):
    event.pop("cash_cost")
    event["allocated_cost"] = {"amount": "0.15", "currency": "USD", "source": "Explicit subscription allocation"}
    ledger.record(event)
    assert ledger.summary("jack")["cash_cost"]["reported"] == []


def test_quota_is_not_money(ledger, event):
    event["quota_usage"] = {"amount": "2", "unit": "requests", "source": "Explicit entitlement meter"}
    ledger.record(event)
    assert ledger.summary("jack")["quota_usage"]["reported"] == [{"unit": "requests", "amount": "2"}]


def test_partial_cost_does_not_claim_complete(ledger, event):
    second = {**event, "request_id": "gen-2", "cash_cost": None}
    ledger.record_many([event, second])
    assert ledger.summary("jack")["cash_cost"]["complete"] is False


def test_multiple_currencies_remain_separate(ledger, event):
    second = copy.deepcopy(event)
    second.update(request_id="gen-2", cash_cost={"amount": "0.2", "currency": "EUR", "source": "usage.cost"})
    ledger.record_many([event, second])
    assert ledger.summary("jack")["cash_cost"]["reported"] == [{"currency": "EUR", "amount": "0.2"}, {"currency": "USD", "amount": "0.15"}]


def test_observed_rate_uses_explicit_occupied_time(ledger, event):
    ledger.record(event)
    result = ledger.summary("jack", since="2026-01-01T00:00:00Z", until="2026-01-01T01:00:00Z", occupied_seconds="1800")
    assert result["observed_hourly_rate"]["amount"] == "0.30000000"


def test_parallel_wall_times_do_not_supply_denominator(ledger, event):
    event["wall_seconds"] = "100"
    ledger.record(event)
    assert ledger.summary("jack")["observed_hourly_rate"]["amount"] is None


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "3601"])
def test_invalid_occupied_time_is_rejected(ledger, value):
    with pytest.raises(AccountingError):
        ledger.summary("jack", since="2026-01-01T00:00:00Z", until="2026-01-01T01:00:00Z", occupied_seconds=value)


def test_occupied_time_requires_explicit_window(ledger):
    with pytest.raises(AccountingError):
        ledger.summary("jack", occupied_seconds="1")


def test_half_open_window_excludes_upper_bound(ledger, event):
    ledger.record(event)
    assert ledger.list_events(since="2026-01-01T00:00:00Z", until="2026-01-01T00:10:00Z") == []


def test_timezone_offsets_normalize_for_window(ledger, event):
    ledger.record(event)
    assert len(ledger.list_events(since="2026-01-01T01:00:00+01:00", until="2026-01-01T01:20:00+01:00")) == 1


def test_empty_read_does_not_create_database(ledger):
    ledger.list_events()
    assert not ledger.path.exists()


def test_reopened_ledger_preserves_unknown_provider(ledger, event):
    event["provider"] = None
    ledger.record(event)
    assert Ledger(ledger.path.parent).list_events()[0]["provider"] is None


def test_unsupported_content_never_enters_ledger(event):
    event["text"] = "private transcript content"
    with pytest.raises(AccountingError):
        normalize_event(event)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1e1000000", "0.0000000000000000001", 0.15])
def test_invalid_money_is_rejected(event, value):
    event["cash_cost"]["amount"] = value
    with pytest.raises(AccountingError):
        normalize_event(event)


@pytest.mark.parametrize("value", [-1, True, 1.5, "10"])
def test_invalid_tokens_are_rejected(event, value):
    event["tokens"]["input"] = value
    with pytest.raises(AccountingError):
        normalize_event(event)


def test_cash_amount_requires_provenance(event):
    event["cash_cost"].pop("source")
    with pytest.raises(AccountingError):
        normalize_event(event)


def test_corrupt_index_fields_fail_closed(ledger, event):
    ledger.record(event)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("UPDATE usage SET cost_center_id='hidden-owner'")
    with pytest.raises(AccountingError, match="mismatch"):
        ledger.summary("jack")


def test_cross_thread_retries_record_once(ledger, event):
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: ledger.record(event)["status"], range(8)))
    assert results.count("recorded") == 1


def test_cross_process_retries_record_once(ledger, event, tmp_path):
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps(event))
    command = [sys.executable, "-O", "-c", "import json,sys; from one_conv.accounting import Ledger; Ledger(sys.argv[1]).record(json.load(open(sys.argv[2])))", str(ledger.path.parent), str(payload)]
    processes = [subprocess.Popen(command, env=os.environ.copy()) for _ in range(4)]
    results = [process.wait(timeout=15) for process in processes]
    assert (results, len(ledger.list_events())) == ([0, 0, 0, 0], 1)
