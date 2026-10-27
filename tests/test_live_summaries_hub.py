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
"""Block summaries shared through a real local hub: numbered history, requests on receipts, settling, catch-up."""

import socket
import threading

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.live import hubclient, router, team
from one_conv.live.hub import Hub, application
from one_conv.live.model import LiveSession

MAC_SESSION = "claude:c1"
BOX_SESSION = "claude:b1"


def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.02)

    def stop():
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    return f"http://127.0.0.1:{sock.getsockname()[1]}", stop


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub with nodes `mac` (this machine) and `box`, each publishing one session."""
    state = Hub(str(tmp_path / "hub.db"))
    mac, box = state.add_token("mac"), state.add_token("box")
    state.publish("mac", [{"address": MAC_SESSION}])
    state.publish("box", [{"address": BOX_SESSION}])
    url, stop = serve(application(state))
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    monkeypatch.setenv("ONE_CONV_HUB_URL", url)
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", mac)
    monkeypatch.setenv("ONE_CONV_HUB_NODE", "mac")
    yield state, url, box
    stop()


def as_box(url, box):
    return hubclient.HubClient(url, box, "box")


def fill(client, count, start=0):
    """The box posts #start.. on #release; the receipt of the last one."""
    receipt = None
    for index in range(start, start + count):
        receipt = client.post({"id": f"{index:032x}", "to": "#release", "from": BOX_SESSION, "text": f"post {index}"})
    return receipt


def test_history_numbers_the_posts(hub):
    state, url, box = hub
    fill(as_box(url, box), 3)
    assert [row["pos"] for row in state.history("release")] == [0, 1, 2]


def test_history_reads_from_a_position(hub):
    _, url, box = hub
    fill(as_box(url, box), 25)
    assert [row["pos"] for row in as_box(url, box).history("release", pos=20)] == list(range(20, 25))


def test_receipt_asks_for_a_summary_when_a_block_completes(hub):
    _, url, box = hub
    assert fill(as_box(url, box), 16)["summarize"]["block"] == "0-15"


def test_receipt_asks_nothing_before_a_block_completes(hub):
    _, url, box = hub
    assert "summarize" not in fill(as_box(url, box), 15)


def test_topic_send_receipt_carries_the_request(hub):
    _, url, box = hub
    fill(as_box(url, box), 15)
    receipt = router.send("#release", "the sixteenth", LiveSession("claude", "c1"))
    assert receipt["summarize"]["block"] == "0-15"


def test_summarize_settles_the_block(hub):
    _, url, box = hub
    fill(as_box(url, box), 16)
    assert team.summarize("release", "0-15", "agreed on the plan", MAC_SESSION)["settled"] is True


def test_summarize_records_the_author_route(hub):
    state, url, box = hub
    fill(as_box(url, box), 16)
    team.summarize("release", "0-15", "agreed on the plan", MAC_SESSION)
    assert state.summaries("release")["summaries"][0]["by"] == "mac/claude:c1"


def test_summarize_keeps_the_first_line(hub):
    _, url, box = hub
    fill(as_box(url, box), 16)
    team.summarize("release", "0-15", "first", MAC_SESSION)
    assert team.summarize("release", "0-15", "second", MAC_SESSION)["existing"] == "first"


def test_summarize_needs_a_published_author(hub):
    _, url, box = hub
    fill(as_box(url, box), 16)
    with pytest.raises(team.TeamError, match="published"):
        team.summarize("release", "0-15", "agreed", "claude:nobody")


def test_summarize_refuses_an_incomplete_block(hub):
    _, url, box = hub
    fill(as_box(url, box), 10)
    with pytest.raises(team.TeamError, match="not complete"):
        team.summarize("release", "0-15", "too early", MAC_SESSION)


def test_summarize_refuses_a_parent_before_its_halves(hub):
    _, url, box = hub
    fill(as_box(url, box), 32)
    with pytest.raises(team.TeamError, match="halves"):
        team.summarize("release", "0-31", "too early", MAC_SESSION)


def test_summarize_offers_the_next_block(hub):
    _, url, box = hub
    fill(as_box(url, box), 32)
    assert team.summarize("release", "0-15", "first", MAC_SESSION)["pending"]["block"] == "16-31"


def test_pending_request_comes_from_the_hub(hub):
    _, url, box = hub
    fill(as_box(url, box), 16)
    assert team.pending_request("release")["block"] == "0-15"


def test_catch_up_shows_the_summary_in_place_of_its_posts(hub):
    _, url, box = hub
    fill(as_box(url, box), 25)
    team.summarize("release", "0-15", "agreed on the plan", MAC_SESSION)
    team.join("release", MAC_SESSION)
    assert team.catch_up("release", MAC_SESSION)["summaries"] == [{"block": "0-15", "text": "agreed on the plan"}]


def test_catch_up_shows_the_newest_posts_whole(hub):
    _, url, box = hub
    fill(as_box(url, box), 25)
    team.join("release", MAC_SESSION)
    assert [m["pos"] for m in team.catch_up("release", MAC_SESSION)["messages"]] == list(range(16, 25))


def test_catch_up_counts_the_posts_behind_the_summaries(hub):
    _, url, box = hub
    fill(as_box(url, box), 25)
    team.join("release", MAC_SESSION)
    assert team.catch_up("release", MAC_SESSION)["omitted"] == 16


def test_catch_up_asks_for_a_missing_summary(hub):
    _, url, box = hub
    fill(as_box(url, box), 25)
    team.join("release", MAC_SESSION)
    assert team.catch_up("release", MAC_SESSION)["summarize"]["block"] == "0-15"


def test_catch_up_moves_the_cursor_past_everything_shown(hub):
    _, url, box = hub
    fill(as_box(url, box), 25)
    team.join("release", MAC_SESSION)
    team.catch_up("release", MAC_SESSION)
    assert team.catch_up("release", MAC_SESSION)["messages"] == []


def test_catch_up_within_its_budget_has_no_summaries(hub):
    _, url, box = hub
    fill(as_box(url, box), 5)
    team.join("release", MAC_SESSION)
    assert team.catch_up("release", MAC_SESSION)["summaries"] == []


def test_open_block_shows_the_posts_of_a_leaf(hub):
    _, url, box = hub
    fill(as_box(url, box), 16)
    assert [p["pos"] for p in team.open_block("release", "0-15")["posts"]] == list(range(16))


def test_open_block_shows_the_halves_of_a_parent(hub):
    _, url, box = hub
    fill(as_box(url, box), 32)
    team.summarize("release", "0-15", "first", MAC_SESSION)
    assert team.open_block("release", "0-31")["halves"] == [{"block": "0-15", "text": "first"},
                                                           {"block": "16-31", "text": None}]


def test_open_block_refuses_posts_not_exchanged_yet(hub):
    _, url, box = hub
    fill(as_box(url, box), 5)
    with pytest.raises(team.TeamError, match="no post #16"):
        team.open_block("release", "16-31")
