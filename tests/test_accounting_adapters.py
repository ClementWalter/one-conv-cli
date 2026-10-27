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
"""Sanitized fixtures reflect inspected native usage shapes without captured transcript text."""

import copy
import io
import json

import pytest

from one_conv.accounting import AccountingError
from one_conv.accounting.adapters import parse_events, read_rows


@pytest.fixture
def context():
    return {"cost_center_id": "jack", "billing_scope": "example-account"}


@pytest.fixture
def codex_rows():
    usage = {"input_tokens": 100, "cached_input_tokens": 80, "cache_write_input_tokens": 0,
             "output_tokens": 10, "reasoning_output_tokens": 4, "total_tokens": 110}
    return [
        {"type": "session_meta", "payload": {"id": "child", "model_provider": "openai",
         "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent", "depth": 1}}}}},
        {"type": "turn_context", "payload": {"model": "example-codex-model", "turn_id": "turn-1"}},
        {"type": "token_usage_record", "ordinal": 3, "timestamp": "2026-01-01T00:10:00Z",
         "payload": {"response_id": "resp-example", "usage": usage, "thread_token_usage": usage}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:10:00Z",
         "payload": {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}}},
    ]


@pytest.fixture
def claude_rows():
    message = {"id": "msg-example", "role": "assistant", "model": "example-claude-model",
               "content": [{"type": "text", "text": "fixture text excluded from accounting"}], "stop_reason": "tool_use",
               "usage": {"input_tokens": 2, "output_tokens": 160, "cache_read_input_tokens": 24000,
                         "cache_creation_input_tokens": 32000, "output_tokens_details": {"thinking_tokens": 38}}}
    first = {"type": "assistant", "sessionId": "example-session", "uuid": "block-1", "requestId": "req-example",
             "timestamp": "2026-01-01T00:10:00Z", "message": message, "isSidechain": False}
    second = {**first, "uuid": "block-2", "timestamp": "2026-01-01T00:10:01Z"}
    return [first, second]


@pytest.fixture
def omp_rows():
    return [{"type": "session", "id": "example-session"},
            {"type": "message", "id": "m2", "parentId": "m1", "timestamp": "2026-01-01T00:10:00Z",
             "message": {"role": "assistant", "model": "example-model", "provider": "example-provider",
                         "duration": 1234.5, "stopReason": "stop", "usage": {
                             "input": 11, "output": 7, "cacheRead": 99, "cacheWrite": 3, "totalTokens": 120,
                             "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}}}]


@pytest.fixture
def router_rows():
    return [{"id": "gen-example", "model": "example/model", "provider": "example-provider", "created": 1767226200,
             "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                                       "cost": "0.015", "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 5},
                                       "completion_tokens_details": {"reasoning_tokens": 4}}}]


def test_codex_uses_response_identity(codex_rows, context):
    assert parse_events("codex", codex_rows, **context)["events"][0]["request_id"] == "resp-example"


def test_codex_does_not_double_count_mirrored_events(codex_rows, context):
    assert len(parse_events("codex", codex_rows, **context)["events"]) == 1


def test_codex_preserves_cached_input(codex_rows, context):
    assert parse_events("codex", codex_rows, **context)["events"][0]["tokens"]["cached_input"] == 80


def test_codex_child_rolls_up_to_owner(codex_rows, context):
    assert parse_events("codex", codex_rows, **context)["events"][0]["cost_center_id"] == "jack"


def test_codex_preserves_parent_worker(codex_rows, context):
    assert parse_events("codex", codex_rows, **context)["events"][0]["parent_worker_id"] == "codex:parent"


def test_codex_cash_remains_unknown(codex_rows, context):
    assert parse_events("codex", codex_rows, **context)["events"][0]["cash_cost"]["amount"] is None


def test_legacy_codex_repeated_snapshots_count_once(codex_rows, context):
    legacy = [codex_rows[0], codex_rows[1], codex_rows[3], copy.deepcopy(codex_rows[3])]
    assert len(parse_events("codex", legacy, **context)["events"]) == 1


def test_legacy_codex_identity_coverage_is_explicit(codex_rows, context):
    legacy = [codex_rows[0], codex_rows[1], codex_rows[3]]
    assert parse_events("codex", legacy, **context)["events"][0]["coverage"]["identity"] == "source_event"


def test_resumed_codex_keeps_legacy_responses_before_native_records(codex_rows, context):
    old = copy.deepcopy(codex_rows[3])
    old["payload"]["info"]["total_token_usage"] = {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}
    assert len(parse_events("codex", [*codex_rows, old], **context)["events"]) == 2


def test_legacy_codex_missing_totals_fails_closed(codex_rows, context):
    codex_rows[3]["payload"]["info"].pop("total_token_usage")
    with pytest.raises(AccountingError, match="cumulative"):
        parse_events("codex", [codex_rows[0], codex_rows[3]], **context)


def test_claude_repeated_content_blocks_count_once(claude_rows, context):
    assert len(parse_events("claude", claude_rows, **context)["events"]) == 1


def test_claude_keeps_cache_creation(claude_rows, context):
    assert parse_events("claude", claude_rows, **context)["events"][0]["tokens"]["cache_write"] == 32000


def test_claude_unknown_provider_is_null(claude_rows, context):
    assert parse_events("claude", claude_rows, **context)["events"][0]["provider"] is None


def test_claude_discards_conversation_content(claude_rows, context):
    assert "fixture text excluded" not in json.dumps(parse_events("claude", claude_rows, **context))


def test_claude_duplicate_uses_first_timestamp(claude_rows, context):
    assert parse_events("claude", claude_rows, **context)["events"][0]["timestamp"] == "2026-01-01T00:10:00.000000+00:00"


def test_claude_native_subagent_has_distinct_worker(claude_rows, context):
    claude_rows[0]["agentId"] = "reviewer"
    assert parse_events("claude", claude_rows[:1], **context)["events"][0]["worker_id"] == "claude:example-session/subagent:reviewer"


def test_claude_native_subagent_has_parent(claude_rows, context):
    claude_rows[0]["agentId"] = "reviewer"
    assert parse_events("claude", claude_rows[:1], **context)["events"][0]["parent_worker_id"] == "claude:example-session"


def test_claude_mapped_parent_keeps_distinct_child(claude_rows, context):
    claude_rows[0]["agentId"] = "reviewer"
    result = parse_events("claude", claude_rows[:1], worker_id="node/claude:mapped-parent", **context)
    assert result["events"][0]["worker_id"] == "node/claude:mapped-parent/subagent:reviewer"


def test_claude_mapped_parent_is_child_parent(claude_rows, context):
    claude_rows[0]["agentId"] = "reviewer"
    result = parse_events("claude", claude_rows[:1], worker_id="node/claude:mapped-parent", **context)
    assert result["events"][0]["parent_worker_id"] == "node/claude:mapped-parent"


def test_omp_calculated_zero_is_not_cash(omp_rows, context):
    assert parse_events("omp", omp_rows, **context)["events"][0]["cash_cost"]["amount"] is None


def test_omp_calculated_zero_is_preserved_as_estimate(omp_rows, context):
    assert parse_events("omp", omp_rows, **context)["events"][0]["estimated_cost"]["amount"] == "0"


def test_omp_keeps_cache_read(omp_rows, context):
    assert parse_events("omp", omp_rows, **context)["events"][0]["tokens"]["cached_input"] == 99


def test_omp_unknown_duration_unit_does_not_create_walltime(omp_rows, context):
    assert parse_events("omp", omp_rows, **context)["events"][0]["wall_seconds"] is None


def test_router_stores_reported_cash(router_rows, context):
    assert parse_events("openrouter", router_rows, worker_id="worker", **context)["events"][0]["cash_cost"]["amount"] == "0.015"


def test_router_missing_cost_remains_unknown(router_rows, context):
    router_rows[0]["usage"].pop("cost")
    assert parse_events("openrouter", router_rows, worker_id="worker", **context)["events"][0]["cash_cost"]["amount"] is None


def test_router_provider_remains_unknown_if_unreported(router_rows, context):
    router_rows[0].pop("provider")
    assert parse_events("openrouter", router_rows, worker_id="worker", **context)["events"][0]["provider"] is None


def test_router_reasoning_is_not_added_to_output(router_rows, context):
    assert parse_events("openrouter", router_rows, worker_id="worker", **context)["events"][0]["tokens"]["output"] == 10


def test_router_upstream_cost_does_not_replace_account_charge(router_rows, context):
    router_rows[0]["usage"]["cost_details"] = {"upstream_inference_cost": "100"}
    assert parse_events("openrouter", router_rows, worker_id="worker", **context)["events"][0]["cash_cost"]["amount"] == "0.015"


def test_router_generation_metadata_uses_total_cost(context):
    rows = [{"data": {"id": "gen-example", "created_at": "2026-01-01T00:10:00Z", "model": "example/model",
                       "provider_name": "example-provider", "native_tokens_prompt": 100, "native_tokens_completion": 10,
                       "total_cost": "0.015", "upstream_inference_cost": "0.012"}}]
    assert parse_events("openrouter", rows, worker_id="worker", **context)["events"][0]["cash_cost"]["amount"] == "0.015"


def test_sse_final_usage_is_imported_once(router_rows, context):
    source = io.StringIO(': keepalive\ndata: {"id":"gen-example","choices":[]}\n' + "data: " + json.dumps(router_rows[0]) + "\ndata: [DONE]\n")
    assert len(parse_events("openrouter", read_rows(source), worker_id="worker", **context)["events"]) == 1


def test_sse_metadata_from_earlier_chunks_is_preserved(router_rows, context):
    first = {key: router_rows[0][key] for key in ("id", "model", "provider", "created")}
    final = {"id": "gen-example", "usage": router_rows[0]["usage"]}
    assert parse_events("openrouter", [first, final], worker_id="worker", **context)["events"][0]["provider"] == "example-provider"


@pytest.mark.parametrize(("source", "row"), [
    ("codex", {"type": "token_usage_record", "payload": 12}),
    ("claude", {"type": "assistant", "message": 12}),
    ("omp", {"type": "message", "message": 12}),
    ("openrouter", {"id": "x", "usage": {}}),
])
def test_malformed_native_metadata_is_actionable(source, row, context):
    with pytest.raises(AccountingError):
        parse_events(source, [row], **context)


def test_numeric_json_preserves_decimal_precision():
    row = next(read_rows(io.StringIO('{"usage":{"cost":0.123456789012345678}}\n')))
    assert str(row["usage"]["cost"]) == "0.123456789012345678"


@pytest.mark.parametrize("source", ["[]\n", "{broken}\n", "[" * 1500 + "0" + "]" * 1500 + "\n"])
def test_invalid_record_fails_actionably(source):
    with pytest.raises(AccountingError):
        list(read_rows(io.StringIO(source)))


def test_oversize_record_is_rejected():
    with pytest.raises(AccountingError, match="4 MiB"):
        list(read_rows(io.StringIO(" " * (4 * 1024 * 1024 + 1))))


def test_router_requires_worker(router_rows, context):
    with pytest.raises(AccountingError, match="stable worker"):
        parse_events("openrouter", router_rows, **context)
