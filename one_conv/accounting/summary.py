"""Coverage-aware summaries never turn an unreported charge into a zero total."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

from .model import (
    MONEY_FIELDS,
    TOKEN_FIELDS,
    AccountingError,
    decimal_string,
    timestamp,
)


def summarize(events, *, cost_center_id=None, since=None, until=None, occupied_seconds=None):
    """Occupied time is supplied for one owner and window, never summed across parallel workers."""
    occupied = decimal_string(occupied_seconds, "occupied_seconds", optional=True)
    if occupied is not None:
        if not cost_center_id or since is None or until is None:
            raise AccountingError("Observed hourly rates require a cost center, since, until and occupied_seconds")
        delta = datetime.fromisoformat(timestamp(until)) - datetime.fromisoformat(timestamp(since))
        span = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000
        if not 0 < Decimal(occupied) <= span:
            raise AccountingError("occupied_seconds must be positive and fit inside the explicit window")
    result = {"cost_center_id": cost_center_id, "since": since, "until": until,
              "occupied_seconds": occupied, "event_count": len(events),
              "includes_internal_subagents": True,
              "coverage": {"sources": {}, "provider_unknown": sum(event["provider"] is None for event in events),
                           "wall_time_unknown": sum(event["wall_seconds"] is None for event in events)},
              "tokens": {}, "quota_usage": {"reported": [], "unknown_events": 0},
              "notice": "Coverage describes recorded events only; unimported charges and fixed subscriptions are not included."}
    with localcontext() as context:
        context.prec = 60
        for field in MONEY_FIELDS:
            totals = {}
            unknown = 0
            for event in events:
                value = event[field]
                if value["amount"] is None:
                    unknown += 1
                else:
                    totals[value["currency"]] = totals.get(value["currency"], Decimal(0)) + Decimal(value["amount"])
            result[field] = {"reported": [{"currency": currency, "amount": format(amount, "f")} for currency, amount in sorted(totals.items())],
                             "unknown_events": unknown, "complete": bool(events) and unknown == 0}
        for field in TOKEN_FIELDS:
            values = [event["tokens"][field] for event in events]
            known = [value for value in values if value is not None]
            result["tokens"][field] = {"reported_total": sum(known) if known else None, "unknown_events": len(values) - len(known)}
        quota_totals = {}
        for event in events:
            result["coverage"]["sources"][event["source"]] = result["coverage"]["sources"].get(event["source"], 0) + 1
            quota = event["quota_usage"]
            if quota["amount"] is None:
                result["quota_usage"]["unknown_events"] += 1
            else:
                quota_totals[quota["unit"]] = quota_totals.get(quota["unit"], Decimal(0)) + Decimal(quota["amount"])
        result["quota_usage"]["reported"] = [{"unit": unit, "amount": format(amount, "f")} for unit, amount in sorted(quota_totals.items())]
        cash = result["cash_cost"]
        amount, currency = None, None
        if cash["complete"] and len(cash["reported"]) == 1 and occupied is not None:
            currency = cash["reported"][0]["currency"]
            amount = format((Decimal(cash["reported"][0]["amount"]) * 3600 / Decimal(occupied)).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f")
        result["observed_hourly_rate"] = {"amount": amount, "currency": currency, "basis": "reported_cash",
                                          "coverage": "complete" if amount is not None else "unknown",
                                          "metering_basis": "Explicit occupied owner seconds within the selected window"}
    return result
