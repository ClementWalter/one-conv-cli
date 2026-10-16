"""Local agent rate cards keep planning estimates separate from provider billing."""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path

from .live.model import live_home


class RateError(ValueError):
    """A rate card or quote cannot be interpreted without guessing."""


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise RateError(f"{label} must contain 1-1000 characters")
    if any(ord(char) < 32 for char in value):
        raise RateError(f"{label} cannot contain control characters")
    return value.strip()


def _number(value, label, maximum):
    """Bound decimal precision before formatting or multiplying untrusted input."""
    if not isinstance(value, str) or len(value) > 64:
        raise RateError(f"{label} must be a decimal with at most 64 characters")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise RateError(f"{label} must be a finite nonnegative decimal") from None
    if not number.is_finite() or not 0 <= number <= maximum:
        raise RateError(f"{label} must be finite and between 0 and {maximum}")
    if number.as_tuple().exponent < -8 or len(number.as_tuple().digits) > 18:
        raise RateError(f"{label} supports at most 18 digits and 8 decimal places")
    return number if number else Decimal(0)


def make_card(cost_center_id, name, *, aliases=(), addresses=(), hourly_rate=None,
              currency, basis, provenance, metering_basis):
    """A card's all-in hourly rate covers its agent and all internal subagents."""
    if not isinstance(cost_center_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", cost_center_id):
        raise RateError("cost center ID must be 1-64 lowercase letters, digits, '-' or '_'")
    if basis not in ("observed", "allocated", "quoted"):
        raise RateError("basis must be observed, allocated or quoted")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
        raise RateError("currency must be a three-letter code, such as USD")
    if not isinstance(aliases, (list, tuple)) or not isinstance(addresses, (list, tuple)):
        raise RateError("aliases and addresses must be lists or tuples of references")
    normalized_aliases = []
    for alias in aliases:
        alias = _text(alias, "alias").removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", alias):
            raise RateError("alias must match a live name: 1-32 lowercase letters, digits or '-'")
        normalized_aliases.append(f"@{alias}")
    normalized_addresses = []
    for address in addresses:
        address = _text(address, "address")
        prefix, separator, ident = address.partition(":")
        if not separator or not prefix or not ident or any(char.isspace() for char in address):
            raise RateError("address must be an exact agent reference, such as codex:SESSION")
        normalized_addresses.append(address)
    amount = None if hourly_rate is None else format(_number(hourly_rate, "hourly rate", Decimal(1000000000)), "f")
    return {
        "cost_center_id": cost_center_id, "name": _text(name, "name"),
        "aliases": sorted(set(normalized_aliases)), "addresses": sorted(set(normalized_addresses)),
        "includes_internal_subagents": True,
        "hourly_rate": {"amount": amount, "currency": currency.upper(), "basis": basis,
                        "provenance": _text(provenance, "provenance"),
                        "metering_basis": _text(metering_basis, "metering basis")},
    }


def _validate_card(card):
    """Reject malformed public-helper inputs before persistence or financial arithmetic."""
    try:
        rate = card["hourly_rate"]
        if card["includes_internal_subagents"] is not True:
            raise RateError("The hourly rate must include internal subagents")
        validated = make_card(card["cost_center_id"], card["name"], aliases=card["aliases"],
                              addresses=card["addresses"], hourly_rate=rate["amount"],
                              currency=rate["currency"], basis=rate["basis"],
                              provenance=rate["provenance"], metering_basis=rate["metering_basis"])
        if "updated_at" in card:
            stamp = datetime.fromisoformat(card["updated_at"])
            if stamp.tzinfo is None:
                raise RateError("Rate card timestamp requires a timezone")
            validated["updated_at"] = card["updated_at"]
        if card != validated:
            raise RateError("Unexpected or noncanonical rate card fields")
        return validated
    except (ValueError, KeyError, TypeError) as error:
        raise RateError("Invalid rate card; use make_card to construct accounting inputs") from error


def quote(card, minutes):
    """Use the supplied occupied work minutes; no agent is contacted or staffed."""
    card = _validate_card(card)
    duration = _number(minutes, "minutes", Decimal(525600))
    rate = card["hourly_rate"]
    amount = None
    if rate["amount"] is not None:
        with localcontext() as context:
            context.prec = 40
            amount = format((Decimal(rate["amount"]) * duration / 60).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f")
    return {
        "cost_center_id": card["cost_center_id"], "name": card["name"],
        "estimated_minutes": format(duration, "f"),
        "expected_cost": {"amount": amount, "currency": rate["currency"],
                          "status": "unknown" if amount is None else "estimated"},
        "hourly_rate": dict(rate), "includes_internal_subagents": True,
        "calculation": "hourly_rate * estimated_minutes / 60",
        "notice": "Planning estimate in the rate's metering basis; not an invoice or incremental cash charge.",
    }


class RateStore:
    """A local transactional registry; names and addresses are references, not identities."""

    def __init__(self, directory: Path | None = None):
        directory = directory if directory is not None else Path(os.environ.get("ONE_CONV_HR_DIR", live_home() / "hr"))
        self.path = directory / "rates.sqlite3"

    def list(self):
        if not self.path.exists():
            return []
        try:
            # A read must neither create a registry nor interpret URI characters as options.
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                return self._cards(connection)
        except sqlite3.Error as error:
            raise RateError("Cannot read the HR registry; inspect ONE_CONV_HR_DIR or restore its database") from error

    def show(self, reference):
        for card in self.list():
            if reference == card["cost_center_id"] or reference in (*card["aliases"], *card["addresses"]):
                return card
        raise RateError(f"No rate card for {reference!r}; run one-conv hr list")

    @staticmethod
    def _decode(payload):
        try:
            return _validate_card(json.loads(payload))
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise RateError("Invalid saved rate card; restore the HR registry before using its estimates") from error

    def _cards(self, connection):
        """Registry-wide uniqueness prevents an ambiguous reference from pricing the wrong agent."""
        cards, references = [], set()
        for storage_id, payload in connection.execute("SELECT cost_center_id, payload FROM cards ORDER BY cost_center_id"):
            card = self._decode(payload)
            if storage_id != card["cost_center_id"]:
                raise RateError("HR registry identity mismatch; restore its database before using estimates")
            claimed = {*card["aliases"], *card["addresses"]}
            if references.intersection(claimed):
                raise RateError("Ambiguous HR registry references; restore its database before using estimates")
            references.update(claimed)
            cards.append(card)
        return cards

    def register(self, card, *, replace=False, authorize=None):
        card = _validate_card(card)
        card.pop("updated_at", None)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS cards (cost_center_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                rows = self._cards(connection)
                current = next((row for row in rows if row["cost_center_id"] == card["cost_center_id"]), None)
                if authorize is not None:
                    # Ownership is checked under the write lock so concurrent creators cannot replace each other.
                    authorize(current)
                if current and {key: value for key, value in current.items() if key != "updated_at"} == card:
                    return current
                if current and not replace:
                    raise RateError("Cost center already has a different card; use --replace to update it")
                wanted = {*card["aliases"], *card["addresses"]}
                for other in rows:
                    if other["cost_center_id"] != card["cost_center_id"] and wanted.intersection((*other["aliases"], *other["addresses"])):
                        raise RateError(f"An alias or address already belongs to cost center {other['cost_center_id']!r}")
                saved = {**card, "updated_at": datetime.now(UTC).isoformat()}
                connection.execute("INSERT INTO cards VALUES (?, ?) ON CONFLICT(cost_center_id) DO UPDATE SET payload=excluded.payload",
                                   (card["cost_center_id"], json.dumps(saved)))
                return saved
        except sqlite3.Error as error:
            raise RateError("Cannot write the HR registry; inspect ONE_CONV_HR_DIR or restore its database") from error
