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
"""`live send --when idle`: the held store, local sends to a busy session, `live status`, the relay column."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from mcp.client import Client

from one_conv import mcp_server
from one_conv.live import held, router
from one_conv.live.cli import build_live_group
from one_conv.live.hub import Hub
from one_conv.live.model import LiveSession

TARGET = "omp:worker-1"
SENDER = LiveSession("claude", "lead-1", "/lead", "lead")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """One omp session on this machine whose status a test sets; what its adapter receives is recorded."""
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    state = SimpleNamespace(status="busy", listed=True, received=[], bridge=True)

    def listing(harness=None):
        session = LiveSession("omp", "worker-1", "/work", "worker", state.status)
        return [session] if state.listed and harness in (None, "omp") else []
    monkeypatch.setattr(router, "list_all", listing)
    monkeypatch.setitem(router.ADAPTERS, "omp", SimpleNamespace(
        deliver=lambda session, text: state.received.append(text) or "delivered"))
    monkeypatch.setattr(held, "bridge_running", lambda: state.bridge)
    return state


def due(now=None):
    return held.deliver_due(router.list_all, router.ADAPTERS, now=now)


def test_an_idle_send_to_a_busy_session_is_held(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    assert receipt["status"] == f"held until {TARGET} is idle"


def test_an_idle_send_to_a_busy_session_is_not_delivered_yet(agent):
    router.send(TARGET, "after your turn", SENDER, when="idle")
    assert agent.received == []


def test_a_send_now_to_a_busy_session_is_delivered_at_once(agent):
    router.send(TARGET, "steer now", SENDER)
    assert len(agent.received) == 1


def test_an_idle_send_to_an_idle_session_is_delivered_at_once(agent):
    agent.status = "idle"
    router.send(TARGET, "after your turn", SENDER, when="idle")
    assert len(agent.received) == 1


def test_a_session_waiting_on_an_approval_counts_as_busy(agent):
    agent.status = "waiting"
    router.send(TARGET, "after your turn", SENDER, when="idle")
    assert agent.received == []


def test_an_idle_send_to_a_busy_session_without_a_bridge_names_live_serve(agent):
    agent.bridge = False
    with pytest.raises(router.LiveError, match="one-conv live serve"):
        router.send(TARGET, "after your turn", SENDER, when="idle")


def test_an_idle_fyi_is_refused(agent):
    with pytest.raises(router.LiveError, match="fyi"):
        router.send(TARGET, "build is green", SENDER, fyi=True, when="idle")


def test_an_unknown_when_is_refused(agent):
    with pytest.raises(router.LiveError, match="when must be one of"):
        router.send(TARGET, "hello", SENDER, when="later")


def test_an_idle_send_to_a_cloud_conversation_is_refused(agent):
    with pytest.raises(router.LiveError, match="cloud conversation"):
        router.send("claude-chat:abc", "hello", SENDER, when="idle")


def test_a_held_message_is_delivered_once_the_session_is_idle(agent):
    router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.status = "idle"
    due()
    assert len(agent.received) == 1


def test_a_held_message_is_delivered_at_most_once(agent):
    router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.status = "idle"
    due()
    due()
    assert len(agent.received) == 1


def test_a_held_message_keeps_its_message_id(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.status = "idle"
    due()
    assert f'id="{receipt["id"]}"' in agent.received[0]


def test_a_held_message_keeps_the_sender_envelope(agent):
    router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.status = "idle"
    due()
    assert 'from="claude:lead-1"' in agent.received[0]


def test_a_held_message_stays_while_the_session_is_busy(agent):
    router.send(TARGET, "after your turn", SENDER, when="idle")
    due()
    assert agent.received == []


def test_a_delivered_held_message_is_recorded_delivered(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.status = "idle"
    due()
    assert held.lookup(receipt["id"])["status"] == "delivered"


def test_a_held_message_whose_session_ended_expires(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.listed = False
    due(now=time.time() + held.GONE_SECONDS + 1)
    assert held.lookup(receipt["id"])["status"] == "expired"


def test_an_expired_message_says_its_session_ended(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.listed = False
    due(now=time.time() + held.GONE_SECONDS + 1)
    assert held.lookup(receipt["id"])["detail"] == f"{TARGET} ended before it was idle"


def test_a_session_briefly_unlisted_keeps_its_held_message(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.listed = False
    due()
    assert held.lookup(receipt["id"])["status"] == "held"


def test_a_message_held_past_the_limit_expires(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    due(now=time.time() + held.MAX_HOLD_SECONDS + 1)
    assert held.lookup(receipt["id"])["status"] == "expired"


def test_a_message_left_delivering_by_a_stopped_bridge_is_unconfirmed(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    with sqlite3.connect(held.store_path()) as db:
        db.execute("UPDATE held SET status = 'delivering'")
    agent.status = "idle"
    due()
    assert held.lookup(receipt["id"])["status"] == "unconfirmed"


def test_a_message_left_delivering_is_not_delivered_again(agent):
    router.send(TARGET, "after your turn", SENDER, when="idle")
    with sqlite3.connect(held.store_path()) as db:
        db.execute("UPDATE held SET status = 'delivering'")
    agent.status = "idle"
    due()
    assert agent.received == []


def test_an_adapter_failure_on_a_held_message_is_unconfirmed(agent, monkeypatch):
    def fail(session, text):
        raise RuntimeError("socket closed mid-write")
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    monkeypatch.setitem(router.ADAPTERS, "omp", SimpleNamespace(deliver=fail))
    agent.status = "idle"
    due()
    assert held.lookup(receipt["id"])["status"] == "unconfirmed"


def test_holding_the_same_message_twice_keeps_one(agent):
    held.hold("a" * 32, TARGET, "user", "text")
    assert held.hold("a" * 32, TARGET, "user", "text") is False


def test_a_held_send_counts_toward_the_rate_limit(agent):
    for _ in range(router.RATE_LIMIT):
        router.send(TARGET, "after your turn", SENDER, when="idle")
    with pytest.raises(router.LiveError, match="already sent"):
        router.send(TARGET, "one too many", SENDER, when="idle")


def live_status(message_id):
    return CliRunner().invoke(build_live_group(["one-conv"]), ["status", message_id]).output


def test_live_status_shows_a_held_message_waiting_for_idle(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    assert live_status(receipt["id"]) == f"held until {TARGET} is idle\n"


def test_live_status_shows_a_held_message_delivered(agent):
    receipt = router.send(TARGET, "after your turn", SENDER, when="idle")
    agent.status = "idle"
    due()
    assert live_status(receipt["id"]) == f"delivered: {TARGET} (delivered)\n"


def test_live_send_when_idle_prints_the_held_receipt(agent):
    result = CliRunner().invoke(build_live_group(["one-conv"]),
                                ["send", TARGET, "after your turn", "--when", "idle", "--yes"])
    assert result.output.startswith(f"held until {TARGET} is idle: {TARGET}\n")


def test_live_send_dry_run_says_the_message_would_be_held(agent):
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["send", TARGET, "after your turn", "--when", "idle"])
    assert f"when:     held until {TARGET} is idle" in result.output


def test_the_send_message_tool_holds_with_when_idle(agent):
    async def call():
        async with Client(mcp_server.create_server()) as client:
            return await client.call_tool("send_message", {"to": TARGET, "text": "after your turn", "when": "idle"})
    result = asyncio.run(call())
    assert result.structured_content["delivery"] == "held"


def test_an_old_relay_database_gains_the_when_column(tmp_path):
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,"
                   " digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, reply TEXT NOT NULL,"
                   " mode TEXT NOT NULL DEFAULT 'push', text TEXT NOT NULL, created REAL NOT NULL, attachments TEXT,"
                   " UNIQUE (from_node, id))")
    Hub(str(path))
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(posts)")}
    assert "timing" in columns
