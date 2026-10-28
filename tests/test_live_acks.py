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
"""Post, ack and sync: a human post wakes every running session, each acks it, and the human sees who read it.

There are no tasks: a session acks a message it received, or any post of a channel it joined,
and the hub lists the acks with the post. The agent-side footer tells each session to ack and
to agree in the channel who does what.
"""

import json
import sqlite3

import pytest
from click.testing import CliRunner

from one_conv.control.store import ControlStore
from one_conv.live import hubclient, router
from one_conv.live.cli import build_live_group
from one_conv.live.hub import Hub, HubError, application
from one_conv.live.model import LiveSession, envelope
from tests.test_live_bridge import serve_app

HUMAN = "workspace"
POST = "a" * 32


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    state.publish(HUMAN, [{"address": "user"}])
    state.adopt_trusted_human(HUMAN)
    state.publish("mac", [{"address": "claude:one"}, {"address": "codex:two"}, {"address": "omp:out"}])
    state.join("mac", "work", "claude:one")
    state.join("mac", "work", "codex:two")
    state.send(HUMAN, {"id": POST, "from": "user", "to": "#work", "text": "Fix the build", "reply": "expected",
                       "mode": "push"}, join_sender=True)
    return state


def wakes(hub):
    return sorted((item["to"], item["mode"]) for item in hub.inbox("mac", 0))


def test_a_human_post_wakes_every_running_member(hub):
    assert wakes(hub) == [("claude:one", "push"), ("codex:two", "push")]


def test_a_human_post_creates_no_task(hub):
    with sqlite3.connect(hub.path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'tasks'").fetchone() is None


def test_an_ack_is_listed_with_the_post(hub):
    hub.acknowledge("mac", "claude:one", POST)
    assert hub.history("work")[0]["acks"] == ["mac/claude:one"]


def test_acks_are_listed_in_the_order_they_came(hub):
    hub.acknowledge("mac", "codex:two", POST)
    hub.acknowledge("mac", "claude:one", POST)
    assert hub.history("work")[0]["acks"] == ["mac/codex:two", "mac/claude:one"]


def test_acking_again_changes_nothing(hub):
    hub.acknowledge("mac", "claude:one", POST)
    assert hub.acknowledge("mac", "claude:one", POST)["acks"] == ["mac/claude:one"]


def test_an_unacked_post_lists_no_acks(hub):
    assert "acks" not in hub.history("work")[0]


def test_a_session_outside_the_channel_cannot_ack(hub):
    with pytest.raises(HubError):
        hub.acknowledge("mac", "omp:out", POST)


def test_a_session_that_joined_later_can_ack(hub):
    hub.join("mac", "work", "omp:out")
    assert hub.acknowledge("mac", "omp:out", POST)["acked_by"] == "mac/omp:out"


def test_an_unpublished_session_cannot_ack(hub):
    with pytest.raises(HubError):
        hub.acknowledge("mac", "claude:ghost", POST)


def test_a_malformed_message_id_is_refused(hub):
    with pytest.raises(HubError):
        hub.acknowledge("mac", "claude:one", "nope")


def test_an_agent_posts_beside_a_human_post_freely(hub):
    receipt = hub.send("mac", {"id": "b" * 32, "from": "claude:one", "to": "#work", "text": "I take the build"})
    assert receipt["duplicate"] is False


# --- what the agent sees --------------------------------------------------------------------------


@pytest.fixture
def footer():
    return envelope("Fix the build", None, "workspace/user", "expected", POST, "work")


def test_the_footer_asks_for_an_ack_with_the_message_id(footer):
    assert f'ack tool (message_id="{POST}")' in footer


def test_the_footer_asks_the_sessions_to_divide_the_work(footer):
    assert "say in the channel what you take" in footer


def test_an_announcement_asks_for_no_ack():
    assert "ack" not in envelope("FYI", None, "workspace/user", "none", POST, "work").split("</one-conv-message>")[1]


# --- the workspace shows the acks -----------------------------------------------------------------


def test_the_workspace_shows_who_acked(tmp_path):
    store = ControlStore(tmp_path / "store")
    store.hub.publish("mac", [{"address": "claude:one"}])
    store.hub.join("mac", "general", "claude:one")
    store.send({"id": POST, "target": "#general", "text": "Fix the build"})
    store.hub.acknowledge("mac", "claude:one", POST)
    assert store.topic("general")["messages"][-1]["acks"] == ["mac/claude:one"]


def test_an_ack_changes_the_workspace_fingerprint(tmp_path):
    store = ControlStore(tmp_path / "store")
    store.hub.publish("mac", [{"address": "claude:one"}])
    store.hub.join("mac", "general", "claude:one")
    store.send({"id": POST, "target": "#general", "text": "Fix the build"})
    before = store.fingerprint()
    store.hub.acknowledge("mac", "claude:one", POST)
    assert store.fingerprint() != before


# --- acking over HTTP and from the CLI ------------------------------------------------------------


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A hub over HTTP whose `mac` token this machine's bridge configuration points at."""
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    token = state.add_token("mac")
    (tmp_path / "live" / "bridge").mkdir(parents=True)
    (tmp_path / "live" / "bridge" / "hub.json").write_text(json.dumps({"url": url, "node": "mac", "token": token}))
    state.publish(HUMAN, [{"address": "user"}])
    state.adopt_trusted_human(HUMAN)
    state.publish("mac", [{"address": "claude:one"}])
    state.join("mac", "work", "claude:one")
    state.send(HUMAN, {"id": POST, "from": "user", "to": "#work", "text": "Fix the build", "reply": "expected",
                       "mode": "push"}, join_sender=True)
    yield state
    stop()


def test_an_ack_over_http_is_recorded(served):
    router.ack(POST, "claude:one")
    assert served.history("work")[0]["acks"] == ["mac/claude:one"]


def test_the_cli_acks_a_message(served, monkeypatch):
    session = LiveSession("claude", "one")
    monkeypatch.setattr(router, "list_all", lambda harness=None: [session] if harness in (None, "claude") else [])
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["ack", POST, "--as", "claude:one"],
                                catch_exceptions=False)
    assert "read by mac/claude:one" in result.output


def test_acking_without_a_hub_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(hubclient, "configured", lambda: None)
    with pytest.raises(router.LiveError, match="live hub"):
        router.ack(POST, "claude:one")
