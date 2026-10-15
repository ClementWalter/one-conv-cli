"""Topics shared through a real local hub: membership, fan-out, dedup, catch-up, outbox, redirects."""

import json
import os
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
    # A sender that is not a session posts without joining, so the capture sees the POST itself.
    router.send("#release", "go", None, "user")
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


MESSAGE = {"id": "5" * 32, "to": "#release", "from": "claude:c1", "topic": "release", "mode": "push",
           "reply": "expected", "text": "original"}


def _stub_post(monkeypatch, outcome):
    def post(self, message):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(hubclient.HubClient, "post", post)
    return hubclient.HubClient("http://127.0.0.1:9", "t")


@pytest.mark.parametrize("outcome", [hubclient.HubRefused(500, "after commit"), hubclient.HubRefused(503, "busy"),
                                     hubclient.HubRefused(429, "slow down"), hubclient.HubRefused(408, "timeout"),
                                     {}, {"id": "6" * 32, "recipients": []}])
def test_ambiguous_answer_keeps_the_message_queued(tmp_path, monkeypatch, outcome):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.submit(_stub_post(monkeypatch, outcome), dict(MESSAGE))
    assert (hubclient.outbox() / f"{MESSAGE['id']}.json").exists()


@pytest.mark.parametrize("status", [400, 403, 409])
def test_definitive_refusal_removes_the_queued_message(tmp_path, monkeypatch, status):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    with pytest.raises(hubclient.HubRefused):
        hubclient.submit(_stub_post(monkeypatch, hubclient.HubRefused(status, "no")), dict(MESSAGE))
    assert not (hubclient.outbox() / f"{MESSAGE['id']}.json").exists()


def test_receipt_for_the_id_removes_the_queued_message(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.submit(_stub_post(monkeypatch, {"id": MESSAGE["id"], "recipients": []}), dict(MESSAGE))
    assert not (hubclient.outbox() / f"{MESSAGE['id']}.json").exists()


def test_reusing_a_pending_id_with_new_content_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.queue(dict(MESSAGE))
    with pytest.raises(hubclient.PendingConflict):
        hubclient.queue({**MESSAGE, "text": "changed"})


def test_reusing_a_pending_id_keeps_the_original_content(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.queue(dict(MESSAGE))
    with pytest.raises(hubclient.PendingConflict):
        hubclient.queue({**MESSAGE, "text": "changed"})
    assert json.loads((hubclient.outbox() / f"{MESSAGE['id']}.json").read_text())["text"] == "original"


def test_requeueing_identical_pending_work_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.queue(dict(MESSAGE))
    assert hubclient.queue(dict(MESSAGE)).exists()


def _queued(tmp_path, monkeypatch, *ids):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    for message_id in ids:
        hubclient.queue({**MESSAGE, "id": message_id})


def test_drain_posts_queued_messages_and_empties_the_outbox(tmp_path, monkeypatch):
    _queued(tmp_path, monkeypatch, "a" * 32, "b" * 32)
    monkeypatch.setattr(hubclient.HubClient, "post", lambda self, m: {"id": m["id"], "recipients": []})
    hubclient.drain(hubclient.HubClient("http://127.0.0.1:9", "t"))
    assert list(hubclient.outbox().glob("*.json")) == []


def test_drain_keeps_everything_queued_on_a_transient_error(tmp_path, monkeypatch):
    _queued(tmp_path, monkeypatch, "a" * 32, "b" * 32)
    hubclient.drain(_stub_post(monkeypatch, hubclient.HubRefused(503, "busy")))
    assert sorted(p.stem for p in hubclient.outbox().glob("*.json")) == ["a" * 32, "b" * 32]


def test_drain_never_moves_a_transient_failure_to_refused(tmp_path, monkeypatch):
    _queued(tmp_path, monkeypatch, "a" * 32)
    hubclient.drain(_stub_post(monkeypatch, hubclient.HubRefused(503, "busy")))
    assert not (hubclient.outbox() / "refused").exists()


def test_drain_sets_a_definitive_refusal_aside(tmp_path, monkeypatch):
    _queued(tmp_path, monkeypatch, "a" * 32)
    hubclient.drain(_stub_post(monkeypatch, hubclient.HubRefused(400, "bad")))
    assert (hubclient.outbox() / "refused" / f"{'a' * 32}.json").exists()


def test_drain_after_a_transient_error_posts_on_the_next_attempt(tmp_path, monkeypatch):
    _queued(tmp_path, monkeypatch, "a" * 32)
    hubclient.drain(_stub_post(monkeypatch, hubclient.HubRefused(503, "busy")))
    monkeypatch.setattr(hubclient.HubClient, "post", lambda self, m: {"id": m["id"], "recipients": []})
    assert hubclient.drain(hubclient.HubClient("http://127.0.0.1:9", "t"))["sent"] == ["a" * 32]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_answer_keeps_the_message_queued(tmp_path, monkeypatch, status):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.submit(_stub_post(monkeypatch, hubclient.HubRefused(status, "moved")), dict(MESSAGE))
    assert (hubclient.outbox() / f"{MESSAGE['id']}.json").exists()


@pytest.mark.parametrize("rows", [[{}], [None], [{"to": "box/omp:worker"}], [{"status": "stored"}],
                                  [{"to": 1, "status": "stored"}]])
def test_receipt_with_malformed_recipients_keeps_the_message_queued(tmp_path, monkeypatch, rows):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.submit(_stub_post(monkeypatch, {"id": MESSAGE["id"], "recipients": rows}), dict(MESSAGE))
    assert (hubclient.outbox() / f"{MESSAGE['id']}.json").exists()


def test_receipt_with_complete_recipients_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    receipt = {"id": MESSAGE["id"], "recipients": [{"to": "box/omp:worker", "status": "stored"}]}
    hubclient.submit(_stub_post(monkeypatch, receipt), dict(MESSAGE))
    assert not (hubclient.outbox() / f"{MESSAGE['id']}.json").exists()


def test_drain_survives_a_file_removed_while_it_lists_the_outbox(tmp_path, monkeypatch):
    _queued(tmp_path, monkeypatch, "a" * 32, "b" * 32)
    real_stat = type(hubclient.outbox()).stat
    gone = hubclient.outbox() / f"{'a' * 32}.json"

    def stat(self, *args, **kwargs):
        if self == gone and os.path.exists(self):
            os.unlink(self)  # A concurrent submit() got its receipt and removed the file.
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(gone), "stat", stat)
    monkeypatch.setattr(hubclient.HubClient, "post", lambda self, m: {"id": m["id"], "recipients": []})
    assert hubclient.drain(hubclient.HubClient("http://127.0.0.1:9", "t"))["sent"] == ["b" * 32]


def test_rejected_token_keeps_the_message_for_the_rotated_one(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    hubclient.submit(_stub_post(monkeypatch, hubclient.HubRefused(401, "bad token")), dict(MESSAGE))
    assert (hubclient.outbox() / f"{MESSAGE['id']}.json").exists()


def test_queued_message_is_readable_only_by_its_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    old = os.umask(0o022)
    try:
        path = hubclient.queue(dict(MESSAGE))
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o600


def test_hub_topic_post_makes_the_sender_a_member(hub):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    router.send("#release", "go", LiveSession("claude", "c1"))
    assert "mac/claude:c1" in state.topics()[0]["members"]


def test_hub_topic_post_with_no_one_else_there_is_kept(hub):
    _, url, box = hub
    router.send("#release", "first word", LiveSession("claude", "c1"))
    as_box(url, box).join("release", BOX_SESSION)
    assert [m["text"] for m in as_box(url, box).history("release")] == ["first word"]


@pytest.fixture
def flaky(monkeypatch):
    """Make chosen hub calls answer 503 until `flaky.clear()`; the rest reach the real hub."""
    failing = set()
    real = {name: getattr(hubclient.HubClient, name) for name in ("join", "post")}

    def wrap(name):
        def call(self, *args, **kwargs):
            if name in failing:
                raise hubclient.HubRefused(503, "temporarily unavailable")
            return real[name](self, *args, **kwargs)
        return call

    for name in real:
        monkeypatch.setattr(hubclient.HubClient, name, wrap(name))
    return failing


def test_failed_join_and_post_are_both_replayed_by_the_drain(hub, flaky):
    _, url, box = hub
    flaky.update({"join", "post"})
    router.send("#new-topic", "kickoff", LiveSession("claude", "c1"))
    flaky.clear()
    hubclient.drain(hubclient.configured())
    assert [m["text"] for m in as_box(url, box).history("new-topic")] == ["kickoff"]


def test_replayed_join_makes_the_sender_a_member(hub, flaky):
    state, _, _ = hub
    flaky.update({"join", "post"})
    router.send("#new-topic", "kickoff", LiveSession("claude", "c1"))
    flaky.clear()
    hubclient.drain(hubclient.configured())
    assert state.topics()[0]["members"] == ["mac/claude:c1"]


def test_failed_join_alone_posts_nothing_yet(hub, flaky):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    flaky.add("join")
    router.send("#release", "go", LiveSession("claude", "c1"))
    assert state.inbox("box", 0) == []


def test_failed_join_alone_keeps_the_send_queued(hub, flaky):
    _, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    flaky.add("join")
    router.send("#release", "go", LiveSession("claude", "c1"))
    assert len(list(hubclient.outbox().glob("*.json"))) == 1


def test_after_a_failed_join_the_peers_reply_reaches_the_sender(hub, flaky):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    flaky.add("join")
    router.send("#release", "go", LiveSession("claude", "c1"))
    flaky.clear()
    hubclient.drain(hubclient.configured())
    as_box(url, box).post({"id": "e" * 32, "to": "#release", "from": BOX_SESSION, "text": "on it"})
    assert [m["text"] for m in state.inbox("mac", 0)] == ["on it"]


@pytest.mark.parametrize("answer", [{}, None, {"topic": "release"}, {"topic": "other", "member": "mac/claude:c1"},
                                    {"topic": "release", "member": "mac/claude:someone-else"}])
def test_unconfirmed_join_keeps_the_send_queued_and_posts_nothing(hub, monkeypatch, answer):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    monkeypatch.setattr(hubclient.HubClient, "join", lambda self, topic, address: answer)
    router.send("#release", "go", LiveSession("claude", "c1"))
    assert (state.inbox("box", 0), len(list(hubclient.outbox().glob("*.json")))) == ([], 1)


def test_after_an_unconfirmed_join_the_drain_stores_one_post(hub, monkeypatch):
    _, url, box = hub
    real_join = hubclient.HubClient.join
    monkeypatch.setattr(hubclient.HubClient, "join", lambda self, topic, address: {})
    router.send("#empty-answer-topic", "kickoff", LiveSession("claude", "c1"))
    monkeypatch.setattr(hubclient.HubClient, "join", real_join)
    hubclient.drain(hubclient.configured())
    hubclient.drain(hubclient.configured())
    assert [m["text"] for m in as_box(url, box).history("empty-answer-topic")] == ["kickoff"]


def test_after_an_unconfirmed_join_replies_reach_the_sender(hub, monkeypatch):
    state, url, box = hub
    as_box(url, box).join("release", BOX_SESSION)
    real_join = hubclient.HubClient.join
    monkeypatch.setattr(hubclient.HubClient, "join", lambda self, topic, address: {})
    router.send("#release", "go", LiveSession("claude", "c1"))
    monkeypatch.setattr(hubclient.HubClient, "join", real_join)
    hubclient.drain(hubclient.configured())
    as_box(url, box).post({"id": "f" * 32, "to": "#release", "from": BOX_SESSION, "text": "on it"})
    assert [m["text"] for m in state.inbox("mac", 0)] == ["on it"]
