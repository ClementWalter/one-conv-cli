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
"""Channel posts report the rollup of their real per-member hub deliveries, never a read receipt."""

import pytest

from one_conv.control.presence import channel_delivery
from one_conv.control.store import ControlStore

NOW = 1_800_000_000.0


# --- rollup -------------------------------------------------------------------------------------


@pytest.mark.parametrize("outcomes,state", [
    ([], "no_recipients"),
    ([None, None], "stored"),
    (["accepted", "accepted"], "accepted"),
    (["refused"], "refused"),
    (["unconfirmed", "unconfirmed"], "unconfirmed"),
    (["accepted", None], "mixed"),
    (["accepted", "refused"], "mixed"),
])
def test_rollup_state_follows_every_member(outcomes, state):
    assert channel_delivery([(f"n/a:{i}", o, NOW) for i, o in enumerate(outcomes)])["state"] == state


def test_partial_acceptance_is_counted():
    assert channel_delivery([("n/a:1", "accepted", NOW), ("n/a:2", None, None)])["label"] == "Accepted by 1 of 2"


def test_partial_acceptance_shows_refusals_in_the_visible_label():
    assert channel_delivery([("n/a:1", "accepted", NOW), ("n/a:2", "refused", NOW)])["label"] == \
        "Accepted by 1 of 2 · 1 refused"


def test_full_acceptance_is_counted():
    assert channel_delivery([("n/a:1", "accepted", NOW), ("n/a:2", "accepted", NOW)])["label"] == "Accepted by 2 of 2"


def test_detail_lists_each_outcome_count():
    detail = channel_delivery([("n/a:1", "accepted", NOW), ("n/a:2", None, None), ("n/a:3", "refused", NOW)])["detail"]
    assert detail.startswith("1 accepted, 1 awaiting delivery, 1 refused of 3 members.")


def test_acceptance_is_never_a_read_receipt():
    assert channel_delivery([("n/a:1", "accepted", NOW)])["read_at"] is None


def test_full_acceptance_reports_the_last_acceptance_time():
    assert channel_delivery([("n/a:1", "accepted", NOW), ("n/a:2", "accepted", NOW + 5)])["accepted_at"] == NOW + 5


def test_partial_acceptance_reports_no_acceptance_time():
    assert channel_delivery([("n/a:1", "accepted", NOW), ("n/a:2", None, None)])["accepted_at"] is None


def test_each_recipient_is_listed():
    assert channel_delivery([("n/a:1", "accepted", NOW)])["recipients"] == [{"to": "n/a:1", "state": "accepted",
                                                                             "at": NOW}]


# --- through the hub -------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path)
    store.hub.publish("worker", [{"address": "omp:agent"}, {"address": "codex:other"}])
    for address in ("omp:agent", "codex:other"):
        store.hub.join("worker", "release", address)
    store.send({"id": "a" * 32, "target": "#release", "text": "Ship it."})
    return store


def _ack(store, address, outcome):
    delivery = next(row["delivery"] for row in store.hub.inbox("worker", 0) if row["to"] == address)
    store.hub.ack("worker", delivery, outcome)


def test_unacknowledged_channel_post_awaits_delivery(store):
    assert store.topic("release")["messages"][-1]["delivery"]["state"] == "stored"


def test_one_acceptance_shows_partial_delivery(store):
    _ack(store, "omp:agent", "accepted")
    assert store.topic("release")["messages"][-1]["delivery"]["label"] == "Accepted by 1 of 2"


def test_every_acceptance_shows_full_delivery(store):
    _ack(store, "omp:agent", "accepted")
    _ack(store, "codex:other", "accepted")
    assert store.topic("release")["messages"][-1]["delivery"]["state"] == "accepted"


def test_a_refusal_is_not_hidden_by_an_acceptance(store):
    _ack(store, "omp:agent", "accepted")
    _ack(store, "codex:other", "refused")
    assert store.topic("release")["messages"][-1]["delivery"]["counts"]["refused"] == 1


def test_row_status_matches_the_rollup(store):
    _ack(store, "omp:agent", "accepted")
    assert store.topic("release")["messages"][-1]["status"] == "mixed"


def test_recipients_use_browser_routes(store):
    assert [row["to"] for row in store.topic("release")["messages"][-1]["delivery"]["recipients"]] == ["worker/omp:agent", "worker/codex:other"]


def test_post_with_no_other_member_says_so(store):
    store.send({"id": "b" * 32, "target": "#solo", "text": "Notes to self."})
    assert store.topic("solo")["messages"][-1]["delivery"]["state"] == "no_recipients"


def test_other_topics_deliveries_are_not_counted(store):
    store.hub.join("worker", "other", "omp:agent")
    store.send({"id": "c" * 32, "target": "#other", "text": "Elsewhere."})
    assert store.topic("release")["messages"][-1]["delivery"]["total"] == 2


def test_older_posts_reflect_acceptance_recorded_before_the_fix(store):
    """Rows are rolled up when read, so historical acceptances already in the hub appear."""
    _ack(store, "omp:agent", "accepted")
    _ack(store, "codex:other", "accepted")
    store.send({"id": "d" * 32, "target": "#release", "text": "A later post."})
    assert store.topic("release")["messages"][0]["delivery"]["state"] == "accepted"


def test_direct_messages_keep_their_own_receipt(store):
    store.send({"id": "e" * 32, "target": "worker/omp:agent", "text": "Direct."})
    assert "recipients" not in store.direct()["threads"][0]["messages"][-1]["delivery"]
