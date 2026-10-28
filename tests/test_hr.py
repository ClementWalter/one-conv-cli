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
"""Rate-card arithmetic and persistence preserve accounting identity and unknowns."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from one_conv.hr import RateError, RateStore, make_card, quote


@pytest.fixture
def card():
    return make_card("jack", "Jack", aliases=["jack"], addresses=["boat:box/session"],
                     hourly_rate="12", currency="USD", basis="allocated",
                     provenance="Example subscription allocation", metering_basis="Occupied work hour")


@pytest.fixture
def store(tmp_path):
    return RateStore(tmp_path / "HR #? é")


def test_unknown_rate_is_null():
    card = make_card("john", "John", currency="USD", basis="quoted",
                     provenance="Not measured", metering_basis="Occupied hour")
    assert card["hourly_rate"]["amount"] is None


def test_zero_rate_is_known():
    card = make_card("john", "John", hourly_rate="0", currency="USD", basis="quoted",
                     provenance="Example zero marginal rate", metering_basis="Occupied hour")
    assert card["hourly_rate"]["amount"] == "0"


def test_rate_includes_internal_subagents(card):
    assert card["includes_internal_subagents"] is True


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-1", "1e10000000", "1e-10000000",
                                  "1000000001", "0.000000001", "1" * 65, "not-a-number"])
def test_invalid_rate_is_rejected(card, value):
    with pytest.raises(RateError):
        make_card("john", "John", hourly_rate=value, currency="USD", basis="quoted",
                  provenance="Example", metering_basis="Occupied hour")


@pytest.mark.parametrize(("field", "value"), [("cost_center_id", "../escape"), ("name", ""),
                                               ("currency", "US"), ("basis", "invented"),
                                               ("provenance", ""), ("metering_basis", "\n"),
                                               ("aliases", ["bad name"]), ("addresses", ["not-an-address"]),
                                               ("aliases", "jack"), ("addresses", "codex:session")])
def test_invalid_card_metadata_is_rejected(field, value):
    values = {"cost_center_id": "jack", "name": "Jack", "hourly_rate": "12", "currency": "USD",
              "basis": "quoted", "provenance": "Example", "metering_basis": "Occupied hour"}
    values[field] = value
    with pytest.raises(RateError):
        make_card(**values)


def test_empty_read_does_not_create_state(store):
    store.list()
    assert not store.path.parent.exists()


def test_registered_card_survives_reopen(store, card):
    saved = store.register(card)
    assert RateStore(store.path.parent).show("jack") == saved


@pytest.mark.parametrize("reference", ["jack", "@jack", "boat:box/session"])
def test_lookup_uses_exact_references(store, card, reference):
    store.register(card)
    assert store.show(reference)["cost_center_id"] == "jack"


def test_identical_retry_does_not_change_timestamp(store, card):
    saved = store.register(card)
    assert store.register(card) == saved


def test_changed_card_needs_explicit_replace(store, card):
    store.register(card)
    with pytest.raises(RateError, match="--replace"):
        store.register({**card, "name": "Renamed Jack"})


def test_rename_keeps_cost_center_identity(store, card):
    store.register(card)
    saved = store.register({**card, "name": "Renamed Jack", "aliases": ["@new-jack"]}, replace=True)
    assert saved["cost_center_id"] == "jack"


def test_replacement_releases_old_reference(store, card):
    store.register(card)
    store.register({**card, "aliases": ["@new-jack"]}, replace=True)
    with pytest.raises(RateError, match="No rate card"):
        store.show("@jack")


@pytest.mark.parametrize("field", ["aliases", "addresses"])
def test_references_cannot_belong_to_two_cost_centers(store, card, field):
    store.register(card)
    other = {**card, "cost_center_id": "john", "aliases": [], "addresses": []}
    other[field] = card[field]
    with pytest.raises(RateError, match="already belongs"):
        store.register(other)


def test_missing_lookup_is_actionable(store):
    with pytest.raises(RateError, match="one-conv hr list"):
        store.show("unknown")


def test_corrupt_registry_fails_visibly(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"not sqlite")
    with pytest.raises(RateError, match="Cannot read"):
        store.list()


@pytest.mark.parametrize("payload", ["null", "[]", "{}", '{"hourly_rate": "broken"}', "[" * 1500 + "0" + "]" * 1500])
def test_malformed_saved_card_is_rejected(store, card, payload):
    store.register(card)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE cards SET payload=?", (payload,))
    with pytest.raises(RateError, match="Invalid saved rate card"):
        store.list()


def test_quote_uses_fractional_minutes_exactly(card):
    assert quote(card, "2.5")["expected_cost"]["amount"] == "0.50000000"


def test_quote_retains_accounting_metadata(card):
    assert quote(card, "15")["hourly_rate"] == card["hourly_rate"]


def test_quote_is_labeled_estimated(card):
    assert quote(card, "15")["expected_cost"]["status"] == "estimated"


@pytest.mark.parametrize("minutes", ["0", "15"])
def test_unknown_quote_never_becomes_zero(card, minutes):
    card["hourly_rate"]["amount"] = None
    assert quote(card, minutes)["expected_cost"]["amount"] is None


def test_zero_quote_is_explicit(card):
    card["hourly_rate"]["amount"] = "0"
    assert quote(card, "15")["expected_cost"] == {"amount": "0.00000000", "currency": "USD", "status": "estimated"}


def test_quote_uses_decimal_rounding(card):
    card["hourly_rate"]["amount"] = "0.1"
    assert quote(card, "1")["expected_cost"]["amount"] == "0.00166667"


def test_quote_preserves_currency(card):
    card["hourly_rate"]["currency"] = "EUR"
    assert quote(card, "1")["expected_cost"]["currency"] == "EUR"


@pytest.mark.parametrize("minutes", ["NaN", "Infinity", "-1", "525601", "1e10000000", "0.000000001", "1" * 65])
def test_invalid_minutes_are_rejected(card, minutes):
    with pytest.raises(RateError):
        quote(card, minutes)


def test_saved_subagent_invariant_is_validated(store, card):
    saved = store.register(card)
    saved["includes_internal_subagents"] = False
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE cards SET payload=?", (json.dumps(saved),))
    with pytest.raises(RateError, match="Invalid saved rate card"):
        store.list()


def test_register_rejects_invalid_helper_input(store, card):
    card["hourly_rate"]["amount"] = "NaN"
    with pytest.raises(RateError, match="Invalid rate card"):
        store.register(card)


def test_quote_rejects_invalid_helper_input(card):
    card["hourly_rate"]["amount"] = "Infinity"
    with pytest.raises(RateError, match="Invalid rate card"):
        quote(card, "15")


def test_negative_zero_is_canonical_zero():
    card = make_card("john", "John", hourly_rate="-0", currency="USD", basis="quoted",
                     provenance="Example", metering_basis="Occupied hour")
    assert card["hourly_rate"]["amount"] == "0"


def test_numeric_subagent_flag_is_not_boolean_true(card):
    card["includes_internal_subagents"] = 1
    with pytest.raises(RateError, match="Invalid rate card"):
        quote(card, "15")


def test_storage_key_must_match_cost_center_id(store, card):
    store.register(card)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE cards SET cost_center_id='storage-only'")
    with pytest.raises(RateError, match="identity mismatch"):
        store.show("jack")


@pytest.mark.parametrize("field", ["aliases", "addresses"])
def test_ambiguous_persisted_references_fail_closed(store, card, field):
    store.register(card)
    other = {**card, "cost_center_id": "john", "aliases": ["@john"], "addresses": ["codex:john"]}
    saved = store.register(other)
    saved[field] = card[field]
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE cards SET payload=? WHERE cost_center_id='john'", (json.dumps(saved),))
    with pytest.raises(RateError, match="Ambiguous"):
        store.show(card[field][0])


def test_registration_refuses_a_mismatched_registry(store, card):
    store.register(card)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE cards SET cost_center_id='storage-only'")
    with pytest.raises(RateError, match="identity mismatch"):
        store.register(card, replace=True)


def test_locked_cards_preserves_the_validated_snapshot(store, card):
    saved = store.register(card)
    with store.locked_cards() as cards:
        assert cards == [saved]


def test_locked_cards_allows_an_unconfigured_registry(store):
    with store.locked_cards() as cards:
        assert cards == []


def test_locked_cards_blocks_other_database_writers(store, card):
    store.register(card)
    with store.locked_cards(), sqlite3.connect(store.path, timeout=0) as connection, pytest.raises(sqlite3.OperationalError, match="locked"):
        connection.execute("BEGIN IMMEDIATE")


@pytest.mark.parametrize("reference", ["jack", "@jack", "boat:box/session"])
def test_rate_update_resolves_existing_references(store, card, reference):
    store.register(card)
    updated = store.update_rate(reference, {**card["hourly_rate"], "amount": "5"})
    assert updated["hourly_rate"]["amount"] == "5"


def test_rate_update_preserves_latest_worker_bindings(store, card):
    store.register({**card, "addresses": []})
    store.register(card, replace=True)
    updated = store.update_rate("jack", {**card["hourly_rate"], "amount": "5"})
    assert updated["addresses"] == card["addresses"]


@pytest.mark.parametrize("field", ["cost_center_id", "name", "aliases", "includes_internal_subagents"])
def test_rate_update_preserves_identity(store, card, field):
    store.register(card)
    assert store.update_rate("jack", {**card["hourly_rate"], "amount": "5"})[field] == card[field]


@pytest.mark.parametrize("amount", [None, "0", "5"])
def test_rate_update_preserves_unknown_vs_zero(store, card, amount):
    store.register(card)
    assert store.update_rate("jack", {**card["hourly_rate"], "amount": amount})["hourly_rate"]["amount"] == amount


@pytest.mark.parametrize("rate", [{}, {"amount": "5"}, {"amount": "NaN"}, None])
def test_invalid_partial_rates_leave_existing_card_unchanged(store, card, rate):
    saved = store.register(card)
    try:
        store.update_rate("jack", rate)
    except RateError:
        pass
    assert store.show("jack") == saved


def test_identical_rate_update_is_idempotent(store, card):
    saved = store.register(card)
    assert store.update_rate("jack", card["hourly_rate"]) == saved


def test_rate_update_requires_an_existing_card(store, card):
    with pytest.raises(RateError, match="No rate card"):
        store.update_rate("jack", card["hourly_rate"])


@pytest.mark.parametrize(("field", "value"), [("amount", "NaN"), ("currency", "invalid"),
                                               ("basis", "invented"), ("provenance", ""), ("extra", "forbidden")])
def test_rate_update_rejects_invalid_complete_rate(store, card, field, value):
    store.register(card)
    with pytest.raises(RateError):
        store.update_rate("jack", {**card["hourly_rate"], field: value})


def test_binding_creates_a_missing_unknown_rate(store, card):
    card["hourly_rate"]["amount"] = None
    assert store.bind_worker(card, "worker/codex:new")["hourly_rate"]["amount"] is None


def test_binding_uses_latest_human_rate(store, card):
    default = {**card, "hourly_rate": {**card["hourly_rate"], "amount": None}}
    store.register(card)
    store.update_rate("jack", {**card["hourly_rate"], "amount": "5"})
    assert store.bind_worker(default, "worker/codex:new")["hourly_rate"]["amount"] == "5"


@pytest.mark.parametrize("field", ["name", "aliases"])
def test_binding_preserves_human_identity_labels(store, card, field):
    store.register(card)
    default = {**card, "name": "default", "aliases": []}
    assert store.bind_worker(default, "worker/codex:new")[field] == card[field]


def test_binding_appends_to_existing_addresses(store, card):
    store.register(card)
    assert store.bind_worker(card, "worker/codex:new")["addresses"] == ["boat:box/session", "worker/codex:new"]


def test_repeated_binding_is_idempotent(store, card):
    saved = store.bind_worker(card, "worker/codex:new")
    assert store.bind_worker(card, "worker/codex:new") == saved


def test_binding_cannot_take_another_cost_centers_address(store, card):
    other = {**card, "cost_center_id": "john", "aliases": ["@john"], "addresses": ["worker/codex:new"]}
    store.register(other)
    with pytest.raises(RateError, match="another cost center"):
        store.bind_worker(card, "worker/codex:new")


def test_concurrent_rate_and_binding_preserve_both_changes(store, card):
    store.register(card)
    barrier = Barrier(2)

    def price():
        barrier.wait(timeout=2)
        return store.update_rate("jack", {**card["hourly_rate"], "amount": "5"})

    def bind():
        barrier.wait(timeout=2)
        return store.bind_worker(card, "worker/codex:new")

    with ThreadPoolExecutor(max_workers=2) as pool:
        priced, bound = pool.submit(price), pool.submit(bind)
        priced.result(timeout=5)
        bound.result(timeout=5)
    saved = store.show("jack")
    assert (saved["hourly_rate"]["amount"], saved["addresses"]) == ("5", ["boat:box/session", "worker/codex:new"])
