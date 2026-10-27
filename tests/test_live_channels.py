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
"""Channels without a roster: members are the running sessions that joined, and work outlives sessions.

Nothing joins a channel by itself, a member leaves its channels once its session has stopped
being published for a grace period, and every session that posted stays listed as a participant.
"""


import pytest

from one_conv.live import hub as hub_module
from one_conv.live.hub import Hub

HUMAN = "workspace"


def post(hub, ident, to="#work", text="Fix the build", sender="user", node=HUMAN, **extra):
    return hub.send(node, {"id": ident * 32, "from": sender, "to": to, "text": text, "reply": "expected",
                           "mode": "push", **extra}, join_sender=node == HUMAN and to.startswith("#"))


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    state.publish(HUMAN, [{"address": "user"}])
    state.adopt_trusted_human(HUMAN)
    state.publish("mac", [{"address": "claude:one"}, {"address": "codex:two"}])
    state.join("mac", "work", "claude:one")
    state.join("mac", "work", "codex:two")
    return state


def topic(hub, name="work"):
    return next(entry for entry in hub.topics() if entry["topic"] == name)


def test_a_published_session_joins_no_channel_by_itself(hub):
    hub.publish("pc", [{"address": "omp:fresh"}])
    assert all("pc/omp:fresh" not in entry["members"] for entry in hub.topics())


def test_a_channel_counts_its_running_members(hub):
    assert topic(hub)["running"] == 2


def test_a_stopped_session_is_no_longer_a_channel_member(hub):
    hub.publish("mac", [{"address": "claude:one"}])
    assert "mac/codex:two" not in topic(hub)["members"]


def test_a_stopped_session_keeps_its_membership_through_the_grace_period(hub):
    hub.publish("mac", [{"address": "claude:one"}])
    hub.publish("mac", [{"address": "claude:one"}, {"address": "codex:two"}])
    assert "mac/codex:two" in topic(hub)["members"]


def test_a_member_stopped_past_the_grace_period_leaves_its_channels(hub, monkeypatch):
    monkeypatch.setattr(hub_module, "MEMBER_GRACE_SECONDS", -1.0)
    hub.publish("mac", [{"address": "claude:one"}])
    hub.publish("mac", [{"address": "claude:one"}, {"address": "codex:two"}])
    assert "mac/codex:two" not in topic(hub)["members"]


def test_a_session_that_posted_stays_a_participant_after_it_stops(hub, monkeypatch):
    monkeypatch.setattr(hub_module, "MEMBER_GRACE_SECONDS", -1.0)
    post(hub, "b", sender="codex:two", node="mac")
    hub.publish("mac", [{"address": "claude:one"}])
    assert topic(hub)["participants"] == ["mac/codex:two"]


def test_a_channel_post_to_nobody_running_is_stored(hub):
    receipt = post(hub, "c", to="#quiet", sender="claude:one", node="mac")
    assert receipt["recipients"] == []
