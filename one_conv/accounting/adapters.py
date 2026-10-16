"""Transcript adapters extract accounting metadata without retaining conversation content."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from .model import AccountingError, normalize_event, text

CODEX_FIELDS = {"input": "input_tokens", "output": "output_tokens", "cached_input": "cached_input_tokens",
                "cache_write": "cache_write_input_tokens", "reasoning": "reasoning_output_tokens", "total": "total_tokens"}


def _raw_decimal(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AccountingError("Reported cost must be a decimal number or string")
    return str(value)


def _stamp(value):
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(int(value), UTC).isoformat()
        except (ValueError, OverflowError, OSError) as error:
            raise AccountingError("Invalid provider timestamp") from error
    return value


def _tokens(usage, fields):
    if not isinstance(usage, dict):
        raise AccountingError("Usage must be a JSON object")
    return {key: usage.get(native) for key, native in fields.items()}


def _cumulative_signature(total):
    keys = ("input_tokens", "output_tokens", "total_tokens")
    if not isinstance(total, dict) or any(type(total.get(key)) is not int or total[key] < 0 for key in keys):
        raise AccountingError("Codex cumulative counters require nonnegative input, output and total tokens")
    return json.dumps({key: total[key] for key in keys}, sort_keys=True)


def _base(context, source_event_id, stamp, **values):
    worker = values.pop("worker_id", None) or context.get("worker_id")
    if not worker or not source_event_id or stamp is None:
        raise AccountingError("Usage requires a stable worker, source event identity and timestamp")
    return {"billing_scope": context["billing_scope"], "source": context["source"],
            "source_event_id": source_event_id, "cost_center_id": context["cost_center_id"],
            "worker_id": worker, "parent_worker_id": context.get("parent_worker_id"),
            "timestamp": _stamp(stamp), "provenance": f"{context['source']} reported usage metadata", **values}


def _codex(rows, context):
    session, provider, model, parent = None, None, None, context.get("parent_worker_id")
    modern, legacy, totals_seen, modern_totals = [], [], set(), set()
    for row in rows:
        payload = row.get("payload") or {}
        if row.get("type") == "session_meta":
            session = text(payload.get("id") or payload.get("session_id"), "Codex session ID", optional=True)
            provider = payload.get("model_provider")
            origin = payload.get("source")
            spawn = ((origin.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(origin, dict) else {}
            if parent is None and spawn.get("parent_thread_id"):
                parent = f"codex:{spawn['parent_thread_id']}"
        elif row.get("type") == "turn_context":
            model = payload.get("model")
        elif row.get("type") == "token_usage_record":
            usage = payload.get("usage")
            if usage is None:
                continue
            response = payload.get("response_id")
            ordinal = row.get("ordinal")
            qualifier = session or context.get("worker_id")
            fallback = f"{qualifier}:ordinal:{ordinal}" if qualifier and type(ordinal) is int else None
            modern.append(_base(context, response or fallback, row.get("timestamp"),
                                worker_id=context.get("worker_id") or (f"codex:{session}" if session else None),
                                request_id=response, provider=provider, model=model, parent_worker_id=parent,
                                tokens=_tokens(usage, CODEX_FIELDS)))
            if payload.get("thread_token_usage") is not None:
                modern_totals.add(_cumulative_signature(payload["thread_token_usage"]))
        elif row.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            usage, total = info.get("last_token_usage"), info.get("total_token_usage")
            if usage is None:
                continue
            if not isinstance(total, dict) or not total:
                raise AccountingError("Legacy Codex usage requires cumulative counters for safe deduplication")
            signature = _cumulative_signature(total)
            if signature in totals_seen:
                continue
            totals_seen.add(signature)
            qualifier = session or context.get("worker_id")
            source_id = f"{qualifier}:cumulative:{hashlib.sha256(signature.encode()).hexdigest()}" if qualifier else None
            legacy.append((signature, _base(context, source_id, row.get("timestamp"),
                                            worker_id=context.get("worker_id") or (f"codex:{session}" if session else None),
                                            provider=provider, model=model, parent_worker_id=parent, tokens=_tokens(usage, CODEX_FIELDS))))
    # Cumulative identities remove mirrored snapshots while preserving older responses in resumed sessions.
    return modern + [event for signature, event in legacy if signature not in modern_totals]


def _claude(rows, context):
    events = {}
    for row in rows:
        message = row.get("message") or {}
        if row.get("type") != "assistant" or message.get("usage") is None:
            continue
        usage = message["usage"]
        request = message.get("id") or row.get("requestId")
        source_id = request or row.get("uuid")
        session = text(row.get("sessionId") or row.get("session_id"), "Claude session ID", optional=True)
        agent = text(row.get("agentId"), "Claude subagent ID", optional=True)
        base_worker = context.get("worker_id") or (f"claude:{session}" if session else None)
        worker = f"{base_worker}/subagent:{agent}" if base_worker and agent else base_worker
        parent = base_worker if agent else context.get("parent_worker_id")
        tokens = _tokens(usage, {"input": "input_tokens", "output": "output_tokens",
                                 "cached_input": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"})
        tokens["reasoning"] = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
        event = _base(context, source_id, row.get("timestamp"), request_id=request,
                      worker_id=worker, parent_worker_id=parent,
                      provider=message.get("provider"), model=message.get("model"), tokens=tokens)
        if source_id in events:
            # Content blocks repeat one response's usage; preserve its first timestamp and final counters.
            event["timestamp"] = events[source_id]["timestamp"]
        events[source_id] = event
    return list(events.values())


def _omp(rows, context):
    events, session = [], None
    for row in rows:
        if row.get("type") == "session":
            session = text(row.get("id"), "OMP session ID", optional=True)
        message = row.get("message") or {}
        if row.get("type") != "message" or message.get("role") != "assistant" or message.get("usage") is None:
            continue
        usage = message["usage"]
        qualifier = session or context.get("worker_id")
        message_id = text(row.get("id"), "OMP message ID", optional=True)
        source_id = message.get("responseId") or (f"{qualifier}:{message_id}" if qualifier and message_id else None)
        estimate = (usage.get("cost") or {}).get("total")
        events.append(_base(context, source_id, row.get("timestamp"), request_id=message.get("responseId"),
                            worker_id=context.get("worker_id") or (f"omp:{session}" if session else None),
                            provider=message.get("provider"), model=message.get("model"),
                            tokens=_tokens(usage, {"input": "input", "output": "output", "cached_input": "cacheRead",
                                                   "cache_write": "cacheWrite", "total": "totalTokens"}),
                            estimated_cost={"amount": _raw_decimal(estimate), "currency": "USD" if estimate is not None else None,
                                            "source": "omp configured model-price estimate" if estimate is not None else None}))
    return events


def _openrouter(rows, context):
    events, metadata = {}, {}
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) else row
        request = data.get("id")
        if request:
            previous = metadata.setdefault(request, {})
            previous.update({key: data[key] for key in ("model", "provider", "provider_name", "created", "created_at") if data.get(key) is not None})
        generation = "total_cost" in data or "native_tokens_prompt" in data
        usage = data.get("usage")
        if not generation and not isinstance(usage, dict):
            continue
        usage = usage if isinstance(usage, dict) else {}
        if not request:
            raise AccountingError("OpenRouter usage requires its generation ID")
        amount = data.get("total_cost") if generation else usage.get("cost")
        tokens = (_tokens(data, {"input": "native_tokens_prompt", "output": "native_tokens_completion",
                                 "cached_input": "native_tokens_cached", "reasoning": "native_tokens_reasoning"})
                  if generation else _tokens(usage, {"input": "prompt_tokens", "output": "completion_tokens", "total": "total_tokens"}))
        if not generation:
            tokens.update(cached_input=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                          cache_write=(usage.get("prompt_tokens_details") or {}).get("cache_write_tokens"),
                          reasoning=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        meta = metadata[request]
        events[request] = _base(context, request, meta.get("created_at") or meta.get("created"), request_id=request,
                                provider=meta.get("provider_name") or meta.get("provider"),
                                model=meta.get("model"), tokens=tokens,
                                cash_cost={"amount": _raw_decimal(amount), "currency": "USD" if amount is not None else None,
                                           "source": "openrouter total_cost" if generation else "openrouter usage.cost"})
    return list(events.values())


def parse_events(source, rows, *, cost_center_id, billing_scope, worker_id=None, parent_worker_id=None):
    """The explicit cost center owns every imported worker, including internal subagents."""
    parsers = {"codex": _codex, "claude": _claude, "omp": _omp, "openrouter": _openrouter}
    context = {"source": source, "cost_center_id": cost_center_id, "billing_scope": billing_scope,
               "worker_id": worker_id, "parent_worker_id": parent_worker_id}
    try:
        if source == "event":
            events = [normalize_event(row) for row in rows]
        elif source in parsers:
            events = [normalize_event(event) for event in parsers[source](rows, context)]
        else:
            raise AccountingError("source must be codex, claude, omp, openrouter or event")
    except AccountingError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError) as error:
        raise AccountingError("Malformed native usage metadata; no records imported") from error
    unique = {}
    for event in events:
        previous = unique.get(event["event_id"])
        if previous is not None and previous != event:
            raise AccountingError("Conflicting usage observations in one import")
        unique[event["event_id"]] = event
    events = list(unique.values())
    return {"events": events, "coverage": {
        "usage_events": len(events), "cash_unknown_events": sum(event["cash_cost"]["amount"] is None for event in events),
        "source_identity_events": sum(event["request_id"] is None for event in events),
        "notice": "Coverage describes imported usage only. Transcript tokens do not establish subscription quota or cash charges; unreported wall time remains unknown.",
    }}


def read_rows(stream):
    """Read bounded JSONL or SSE records, preserving numeric decimals before normalization."""
    for number in range(100001):
        line = stream.readline(4 * 1024 * 1024 + 1)
        if not line:
            return
        if number == 100000 or len(line) > 4 * 1024 * 1024:
            raise AccountingError("Import exceeds 100000 records or 4 MiB per record; split the input")
        line = line.strip()
        if not line or line.startswith(":") or line == "data: [DONE]":
            continue
        line = line.removeprefix("data: ")
        try:
            row = json.loads(line, parse_float=Decimal)
        except (ValueError, RecursionError) as error:
            raise AccountingError(f"Invalid JSON usage record at line {number + 1}") from error
        if not isinstance(row, dict):
            raise AccountingError(f"Usage record at line {number + 1} must be an object")
        yield row
