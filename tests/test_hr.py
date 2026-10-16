"""Rate-card arithmetic and persistence preserve accounting identity and unknowns."""

import json
import sqlite3

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
