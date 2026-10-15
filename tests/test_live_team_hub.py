"""Topics shared through a real local hub: membership, fan-out, dedup, catch-up, outbox, redirects."""

import json
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
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
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
    state.publish("mac", [{"address": MAC_SESSION}, {"address": "claude:c2"}])
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


def test_join_registers_the_member_on_the_hub(hub):
    state, _, _ = hub
    team.join("release", MAC_SESSION)
    assert state.topics()[0]["members"] == ["mac/claude:c1"]


def test_join_of_an_unpublished_session_points_to_live_serve(hub):
    with pytest.raises(team.TeamError, match="live serve"):
        team.join("release", "claude:not-published")


def test_topic_send_reaches_members_on_every_node(hub):
    _, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    team.join("release", "claude:c2")
    receipt = router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert sorted(row["to"] for row in receipt["deliveries"]) == ["box/claude:b1", "mac/claude:c2"]


def test_topic_send_goes_to_the_receiving_node_inbox(hub):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert [m["text"] for m in state.inbox("box", 0)] == ["tests pass"]


def test_retried_topic_id_is_stored_once(hub):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    for _ in range(2):
        router.send("#release", "tests pass", LiveSession("claude", "c1"), message_id="a" * 32)
    assert len(state.inbox("box", 0)) == 1


@pytest.mark.xfail(strict=True, reason="the hub stores a topic post only per recipient, so a post made before "
                   "anyone else joined is lost to later joiners; reported on PR #21")
def test_catch_up_reads_the_hub_history(hub):
    _, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    as_box(url, box).post({"id": "b" * 32, "to": "#release", "from": BOX_SESSION, "text": "from the box"})
    team.join("release", MAC_SESSION)
    assert [m["text"] for m in team.catch_up("release", MAC_SESSION)["messages"]] == ["from the box"]


def test_catch_up_through_the_hub_shows_only_new_messages(hub):
    _, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    team.join("release", MAC_SESSION)
    as_box(url, box).post({"id": "c" * 32, "to": "#release", "from": BOX_SESSION, "text": "first"})
    team.catch_up("release", MAC_SESSION)
    as_box(url, box).post({"id": "d" * 32, "to": "#release", "from": BOX_SESSION, "text": "second"})
    assert [m["text"] for m in team.catch_up("release", MAC_SESSION)["messages"]] == ["second"]


def test_topics_list_comes_from_the_hub(hub):
    _, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    assert [row["topic"] for row in team.topics()] == ["release"]


def test_unreachable_hub_queues_the_topic_message(tmp_path, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # Nothing listens on this port any more.
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    monkeypatch.setenv("ONE_CONV_HUB_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", "t")
    router.send("#release", "later", LiveSession("claude", "c1"), message_id="e" * 32)
    assert json.loads((hubclient.outbox() / f"{'e' * 32}.json").read_text())["to"] == "#release"


def test_redirect_is_refused_rather_than_followed(tmp_path):
    from starlette.applications import Starlette
    from starlette.responses import RedirectResponse
    from starlette.routing import Route

    url, stop = serve(Starlette(routes=[Route("/v1/topics", lambda request: RedirectResponse("http://example.invalid/"))]))
    try:
        with pytest.raises(hubclient.HubRefused):
            hubclient.HubClient(url, "secret").topics()
    finally:
        stop()


def test_no_configuration_means_no_hub(tmp_path, monkeypatch):
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    assert hubclient.configured() is None


def test_bridge_config_file_is_read(tmp_path, monkeypatch):
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    (tmp_path / "live" / "bridge").mkdir(parents=True)
    (tmp_path / "live" / "bridge" / "hub.json").write_text(json.dumps({"url": "http://h", "token": "t", "node": "mac"}))
    assert hubclient.configured().node == "mac"
