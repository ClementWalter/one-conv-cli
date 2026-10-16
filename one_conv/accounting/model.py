"""Canonical usage events preserve unknown values and separate monetary interpretations."""

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

TOKEN_FIELDS = ("input", "output", "cached_input", "cache_write", "reasoning", "total")
MONEY_FIELDS = ("cash_cost", "allocated_cost", "estimated_cost")


class AccountingError(ValueError):
    """Accounting input lacks a safe, explicit interpretation."""


class AccountingConflict(AccountingError):
    """An immutable usage identity already carries different accounting facts."""


def text(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 1000 or any(ord(c) < 32 for c in value):
        raise AccountingError(f"{label} must be a nonempty string of at most 1000 characters without controls")
    return value


def decimal_string(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise AccountingError(f"{label} must be a decimal string, not a JSON number")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise AccountingError(f"{label} must be a finite nonnegative decimal") from None
    if not number.is_finite() or not 0 <= number <= 10**15:
        raise AccountingError(f"{label} must be finite and between 0 and 1000000000000000")
    if number.as_tuple().exponent < -18 or len(number.as_tuple().digits) > 34:
        raise AccountingError(f"{label} supports at most 34 digits and 18 decimal places")
    formatted = format(number if number else Decimal(0), "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def timestamp(value):
    text(value, "timestamp")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError("Timezone required")
        return stamp.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as error:
        raise AccountingError("timestamp must be a timezone-aware ISO 8601 date") from error


def _money(value, label):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"amount", "currency", "source"}:
        raise AccountingError(f"{label} must contain only amount, currency and source")
    amount = decimal_string(value.get("amount"), label, optional=True)
    currency = value.get("currency")
    if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise AccountingError(f"{label} currency must be a three-letter uppercase code")
    source = text(value.get("source"), f"{label} source", optional=True)
    if amount is not None and (currency is None or source is None):
        raise AccountingError(f"{label} requires currency and provenance when an amount is known")
    return {"amount": amount, "currency": currency, "source": source}


def normalize_event(event):
    """Whitelist accounting fields so transcript text and credentials never enter the ledger."""
    required = {"billing_scope", "source", "source_event_id", "cost_center_id", "worker_id", "timestamp", "provenance"}
    optional = {"schema_version", "event_id", "coverage", "request_id", "model", "provider", "parent_worker_id", "node_id",
                "wall_seconds", "tokens", "quota_usage", "field_provenance", *MONEY_FIELDS}
    if not isinstance(event, dict) or required - set(event) or set(event) - required - optional:
        raise AccountingError("Usage event has missing or unsupported fields")
    if type(event.get("schema_version", 1)) is not int or event.get("schema_version", 1) != 1:
        raise AccountingError("Unsupported usage schema version")
    result = {key: text(event[key], key) for key in required - {"timestamp"}}
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", result["cost_center_id"]):
        raise AccountingError("cost_center_id must be a stable lowercase ID of at most 64 characters")
    result["timestamp"] = timestamp(event["timestamp"])
    for key in ("request_id", "model", "provider", "parent_worker_id", "node_id"):
        result[key] = text(event.get(key), key, optional=True)
    result["wall_seconds"] = decimal_string(event.get("wall_seconds"), "wall_seconds", optional=True)
    raw_tokens = event.get("tokens", {})
    if not isinstance(raw_tokens, dict) or set(raw_tokens) - set(TOKEN_FIELDS):
        raise AccountingError("tokens contains unsupported fields")
    result["tokens"] = {}
    for key in TOKEN_FIELDS:
        value = raw_tokens.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= 10**15):
            raise AccountingError(f"tokens.{key} must be a nonnegative integer or null")
        result["tokens"][key] = value
    for key in MONEY_FIELDS:
        result[key] = _money(event.get(key), key)
    quota = {} if event.get("quota_usage") is None else event["quota_usage"]
    if not isinstance(quota, dict) or set(quota) - {"amount", "unit", "source"}:
        raise AccountingError("quota_usage must contain only amount, unit and source")
    result["quota_usage"] = {
        "amount": decimal_string(quota.get("amount"), "quota_usage", optional=True),
        "unit": text(quota.get("unit"), "quota unit", optional=True),
        "source": text(quota.get("source"), "quota source", optional=True),
    }
    if result["quota_usage"]["amount"] is not None and any(result["quota_usage"][key] is None for key in ("unit", "source")):
        raise AccountingError("Known quota usage requires an explicit unit and source")
    identity = ([result["billing_scope"], "request", result["request_id"]] if result["request_id"] else
                [result["billing_scope"], "source", result["source"], result["source_event_id"]])
    result["event_id"] = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    if event.get("event_id", result["event_id"]) != result["event_id"]:
        raise AccountingError("event_id does not match the accounting identity")
    result["schema_version"] = 1
    result["coverage"] = {
        "identity": "provider_request" if result["request_id"] else "source_event",
        "cash_cost": "reported" if result["cash_cost"]["amount"] is not None else "unknown",
        "tokens": "reported" if all(result["tokens"][key] is not None for key in ("input", "output")) else "partial",
        "wall_time": "reported" if result["wall_seconds"] is not None else "unknown",
    }
    known = {key: result[key] for key in ("model", "provider", "wall_seconds", "parent_worker_id")}
    known.update({f"tokens.{key}": value for key, value in result["tokens"].items()})
    known.update({key: result[key]["amount"] for key in (*MONEY_FIELDS, "quota_usage")})
    supplied = event.get("field_provenance", {})
    if not isinstance(supplied, dict) or set(supplied) - {key for key, value in known.items() if value is not None}:
        raise AccountingError("field_provenance must refer only to known accounting fields")
    result["field_provenance"] = {}
    for key, value in known.items():
        if value is None:
            continue
        observation = supplied.get(key, {
            "source": result[key]["source"] if key in (*MONEY_FIELDS, "quota_usage") else result["source"],
            "source_event_id": result["source_event_id"], "provenance": result["provenance"],
        })
        if not isinstance(observation, dict) or set(observation) != {"source", "source_event_id", "provenance"}:
            raise AccountingError("Each field provenance requires source, source_event_id and provenance")
        result["field_provenance"][key] = {name: text(item, "field provenance") for name, item in observation.items()}
    return result


def enrich_event(saved, incoming):
    """Fill unknown evidence without changing known facts, ownership, identity or node attribution."""
    immutable = ("event_id", "billing_scope", "request_id", "cost_center_id", "worker_id", "node_id", "timestamp")
    if any(saved[key] != incoming[key] for key in immutable):
        raise AccountingConflict("Usage identity already has different ownership, node or immutable facts")
    result = deepcopy(saved)
    filled = set()

    def fill(path, old, new):
        if old is not None and new is not None and old != new:
            raise AccountingConflict(f"Usage identity has conflicting known {path}")
        if old is None and new is not None:
            filled.add(path)
            return new
        return old

    for key in ("model", "provider", "wall_seconds", "parent_worker_id"):
        result[key] = fill(key, saved[key], incoming[key])
    for key in TOKEN_FIELDS:
        result["tokens"][key] = fill(f"tokens.{key}", saved["tokens"][key], incoming["tokens"][key])
    for key in (*MONEY_FIELDS, "quota_usage"):
        old, new = saved[key], incoming[key]
        unit = "unit" if key == "quota_usage" else "currency"
        amount = fill(key, old["amount"], new["amount"])
        denomination = fill(f"{key}.{unit}", old[unit], new[unit])
        result[key] = {"amount": amount, unit: denomination,
                       "source": new["source"] if old["amount"] is None and new["amount"] is not None else old["source"] or new["source"]}
    for key in filled:
        if key in incoming["field_provenance"]:
            result["field_provenance"][key] = incoming["field_provenance"][key]
    return normalize_event(result), result != saved
