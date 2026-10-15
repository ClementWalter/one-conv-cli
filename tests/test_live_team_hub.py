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


@pytest.fixture
def capture_server():
    """A second server that records every request it gets, as a redirect target."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    seen = []

    async def record(request):
        seen.append({"auth": request.headers.get("authorization"), "body": (await request.body()).decode()})
        return JSONResponse({})

    url, stop = serve(Starlette(routes=[Route("/{path:path}", record, methods=["GET", "POST", "PUT", "DELETE"])]))
    yield url, seen
    stop()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_sends_neither_token_nor_body_elsewhere(capture_server, code):
    from starlette.applications import Starlette
    from starlette.responses import RedirectResponse
    from starlette.routing import Route

    target, seen = capture_server
    url, stop = serve(Starlette(routes=[Route("/v1/messages", lambda request: RedirectResponse(
        target + "/stolen", status_code=code), methods=["POST"])]))
    try:
        with pytest.raises(hubclient.HubRefused):
            hubclient.HubClient(url, "secret").post({"id": "f" * 32, "to": "#t", "from": "x", "text": "private body"})
    finally:
        stop()
    assert seen == []


@pytest.mark.parametrize("url", ["http://hub.example.com", "http://192.168.1.4:8789", "https://user:pw@hub.example",
                                 "https://tok@hub.example", "ftp://hub.example", "hub.example"])
def test_unsafe_hub_url_is_rejected(url):
    with pytest.raises(ValueError):
        hubclient.check_url(url)


@pytest.mark.parametrize("url", ["https://hub.example", "http://127.0.0.1:8789", "http://[::1]:8789"])
def test_safe_hub_url_is_accepted(url):
    assert hubclient.check_url(url) == url


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
    (tmp_path / "live" / "bridge" / "hub.json").write_text(json.dumps({"url": "http://127.0.0.1:9", "token": "t", "node": "mac"}))
    assert hubclient.configured().node == "mac"


def test_environment_proxy_never_receives_the_hub_request(hub, capture_server, monkeypatch):
    _, url, box = hub
    proxy, seen = capture_server
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", proxy)
    monkeypatch.setenv("HTTP_PROXY", proxy)
    as_box(url, box).join("release", BOX_SESSION)
    assert seen == []


def test_topic_post_carries_a_push_delivery_mode(capture_server, tmp_path, monkeypatch):
    target, seen = capture_server
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    monkeypatch.setenv("ONE_CONV_HUB_URL", target)
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", "t")
    router.send("#release", "go", LiveSession("claude", "c1"))
    assert json.loads(seen[-1]["body"])["mode"] == "push"


def test_fyi_to_a_hub_topic_posts_mode_fyi(hub):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "merged", LiveSession("claude", "c1"), fyi=True)
    assert [m["mode"] for m in state.inbox("box", 0)] == ["fyi"]


def test_fyi_to_a_hub_topic_expects_no_reply(hub):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "merged", LiveSession("claude", "c1"), fyi=True)
    assert [m["reply"] for m in state.inbox("box", 0)] == ["none"]


def test_no_reply_push_to_a_hub_topic_stays_a_push(hub):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "fixed", LiveSession("claude", "c1"), reply="none")
    assert [m["mode"] for m in state.inbox("box", 0)] == ["push"]


def test_queued_fyi_keeps_its_mode(tmp_path, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    monkeypatch.setenv("ONE_CONV_HUB_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", "t")
    router.send("#release", "later", LiveSession("claude", "c1"), fyi=True, message_id="9" * 32)
    assert json.loads((hubclient.outbox() / f"{'9' * 32}.json").read_text())["mode"] == "fyi"


def test_catch_up_pages_past_two_nodes_sharing_a_message_id(hub):
    """Client ids are unique only per node, so the position must be the hub's cursor."""
    _, url, box = hub
    team.join("release", MAC_SESSION)
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "mac says", LiveSession("claude", "c2"), message_id="1" * 32)
    as_box(url, box).post({"id": "1" * 32, "to": "#release", "from": BOX_SESSION, "text": "box says"})
    team.catch_up("release", MAC_SESSION)
    as_box(url, box).post({"id": "2" * 32, "to": "#release", "from": BOX_SESSION, "text": "third"})
    assert [m["text"] for m in team.catch_up("release", MAC_SESSION)["messages"]] == ["third"]


def _bridge_config(tmp_path, monkeypatch, **config):
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN", "ONE_CONV_HUB_NODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    (tmp_path / "live" / "bridge").mkdir(parents=True)
    (tmp_path / "live" / "bridge" / "hub.json").write_text(json.dumps(config))


def test_url_override_without_a_token_is_refused(tmp_path, monkeypatch):
    _bridge_config(tmp_path, monkeypatch, url="http://127.0.0.1:9", token="old", node="mac")
    monkeypatch.setenv("ONE_CONV_HUB_URL", "https://other.example")
    with pytest.raises(hubclient.HubRefused, match="without ONE_CONV_HUB_TOKEN"):
        hubclient.configured()


def test_token_override_rotates_the_configured_hubs_token(tmp_path, monkeypatch):
    _bridge_config(tmp_path, monkeypatch, url="http://127.0.0.1:9", token="old", node="mac")
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", "new")
    assert hubclient.configured().token == "new"


def test_token_override_keeps_the_configured_url(tmp_path, monkeypatch):
    _bridge_config(tmp_path, monkeypatch, url="http://127.0.0.1:9", token="old", node="mac")
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", "new")
    assert hubclient.configured().url == "http://127.0.0.1:9"


def test_token_override_without_any_hub_url_is_refused(tmp_path, monkeypatch):
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_NODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", "new")
    with pytest.raises(hubclient.HubRefused, match="no hub URL"):
        hubclient.configured()


def test_topic_post_is_in_the_outbox_before_the_request(hub, monkeypatch):
    seen = []
    real_post = hubclient.HubClient.post

    def watching(self, message):
        seen.append((hubclient.outbox() / f"{message['id']}.json").exists())
        return real_post(self, message)

    monkeypatch.setattr(hubclient.HubClient, "post", watching)
    _, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "go", LiveSession("claude", "c1"))
    assert seen == [True]


def test_answered_topic_post_leaves_the_outbox_empty(hub):
    _, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "go", LiveSession("claude", "c1"))
    assert list(hubclient.outbox().glob("*.json")) == []


def test_crash_during_the_post_leaves_it_queued_under_its_id(hub, monkeypatch):
    def killed(self, message):
        raise SystemExit("process killed mid-request")

    monkeypatch.setattr(hubclient.HubClient, "post", killed)
    with pytest.raises(SystemExit):
        router.send("#release", "go", LiveSession("claude", "c1"), message_id="7" * 32)
    assert (hubclient.outbox() / f"{'7' * 32}.json").exists()


def test_refused_topic_post_is_not_left_to_retry(hub, monkeypatch):
    def refused(self, message):
        raise hubclient.HubRefused(400, "bad request")

    monkeypatch.setattr(hubclient.HubClient, "post", refused)
    with pytest.raises(router.LiveError):
        router.send("#release", "go", LiveSession("claude", "c1"))
    assert list(hubclient.outbox().glob("*.json")) == []
