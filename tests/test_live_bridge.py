"""The bridge against a real loopback hub: ingress at most once, fyi never wakes, outbox, remote sends, ssh forward.

The local adapter is a stub that records what it is handed, so no agent is prompted.
"""

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.live import bridge, hubclient, router, tunnel
from one_conv.live.hub import Hub, application
from one_conv.live.model import LiveSession

SESSION = LiveSession("omp", "worker-1", "/work", "worker")


def serve_app(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)

    def stop():
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    return f"http://127.0.0.1:{sock.getsockname()[1]}", stop


@pytest.fixture
def hub(tmp_path):
    """A hub with node `mac` (session claude:aaa, named lead) and node `box` (this machine)."""
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    mac = hubclient.HubClient(url, state.add_token("mac"), "mac")
    mac.request("PUT", "/v1/sessions", {"sessions": [{"address": "claude:aaa", "name": "lead", "title": "Lead"}]})
    yield SimpleNamespace(state=state, url=url, mac=mac, box_token=state.add_token("box"))
    stop()


@pytest.fixture
def delivered(tmp_path, monkeypatch):
    """This machine runs SESSION; what its adapter receives is recorded here."""
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN", "ONE_CONV_HUB_NODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(router, "list_all", lambda harness=None: [SESSION] if harness in (None, "omp") else [])
    received = []
    monkeypatch.setitem(router.ADAPTERS, "omp", SimpleNamespace(deliver=lambda s, text: received.append(text) or "queued"))
    return received


@pytest.fixture
def box(hub, delivered):
    client = hubclient.HubClient(hub.url, hub.box_token, "box")
    node = bridge.Bridge(client, "box", bridge.Ingress(bridge.bridge_dir() / "ingress.db"))
    node.publish()
    return node


def post(hub, **extra):
    message = {"id": uuid.uuid4().hex, "to": "box/omp:worker-1", "from": "claude:aaa", "text": "run the tests", **extra}
    hub.mac.post(message)
    return message


def outcome(hub, message):
    return hub.mac.request("GET", f"/v1/messages/{message['id']}")["recipients"][0]["outcome"]


def test_a_push_from_another_machine_reaches_the_local_adapter(hub, box, delivered):
    post(hub)
    box.step(wait=0)
    assert len(delivered) == 1


def test_the_envelope_gives_the_full_return_route(hub, box, delivered):
    post(hub)
    box.step(wait=0)
    assert 'to="mac/claude:aaa"' in delivered[0]


def test_the_envelope_names_the_remote_sender(hub, box, delivered):
    post(hub)
    box.step(wait=0)
    assert 'from-handle="@lead"' in delivered[0]


def test_the_sender_sees_the_adapter_accepted_it(hub, box):
    message = post(hub)
    box.step(wait=0)
    assert outcome(hub, message) == "accepted"


def test_a_delivery_handed_out_again_is_not_delivered_twice(hub, box, delivered):
    post(hub)
    item = box.client.request("GET", "/v1/inbox?wait=0")["messages"][0]
    box.handle(item)
    box.handle(item)
    assert len(delivered) == 1


def test_a_delivery_claimed_before_a_restart_is_reported_unconfirmed(hub, box, delivered):
    message = post(hub)
    item = box.client.request("GET", "/v1/inbox?wait=0")["messages"][0]
    box.ingress.claim(item)  # The previous bridge stopped right after claiming.
    restarted = bridge.Bridge(box.client, "box", bridge.Ingress(bridge.bridge_dir() / "ingress.db"))
    restarted.step(wait=0)
    assert (outcome(hub, message), delivered) == ("unconfirmed", [])


def test_an_fyi_does_not_wake_the_session(hub, box, delivered):
    post(hub, mode="fyi", reply="none")
    box.step(wait=0)
    assert delivered == []


def test_an_fyi_is_filed_for_the_feed(hub, box):
    post(hub, mode="fyi", reply="none", text="build is green")
    box.step(wait=0)
    assert "build is green" in router.fyi_inbox("omp:worker-1").read_text()


def test_a_push_expecting_no_reply_still_wakes(hub, box, delivered):
    post(hub, reply="none")
    box.step(wait=0)
    assert 'reply="none"' in delivered[0]


def test_a_session_not_live_here_is_refused(hub, box, monkeypatch):
    message = post(hub)
    monkeypatch.setattr(router, "list_all", lambda harness=None: [])
    box.step(wait=0)
    assert outcome(hub, message) == "refused"


def test_an_adapter_that_cannot_confirm_is_unconfirmed(hub, box, monkeypatch):
    unknown = "unconfirmed: box prompt exited 1 without Boat accepting or refusing it. Do not resend"
    monkeypatch.setitem(router.ADAPTERS, "omp", SimpleNamespace(deliver=lambda s, text: unknown))
    message = post(hub)
    box.step(wait=0)
    assert outcome(hub, message) == "unconfirmed"


def test_an_adapter_failure_is_unconfirmed(hub, box, monkeypatch):
    def fail(session, text):
        raise RuntimeError("socket closed mid-write")
    monkeypatch.setitem(router.ADAPTERS, "omp", SimpleNamespace(deliver=fail))
    message = post(hub)
    box.step(wait=0)
    assert outcome(hub, message) == "unconfirmed"


def test_publishing_lists_this_machines_sessions_on_the_hub(hub, box):
    assert "box/omp:worker-1" in box.directory


def queued(text="queued while offline"):
    message = {"id": uuid.uuid4().hex, "to": "mac/claude:aaa", "from": "omp:worker-1", "text": text}
    hubclient.queue(message)
    return message


def test_the_outbox_is_posted_under_its_own_id(hub, box):
    message = queued()
    box.drain_outbox()
    assert hub.mac.request("GET", "/v1/inbox?wait=0")["messages"][0]["id"] == message["id"]


def test_a_posted_outbox_message_leaves_the_outbox(hub, box):
    queued()
    box.drain_outbox()
    assert list(hubclient.outbox().glob("*.json")) == []


def test_an_outbox_message_the_hub_already_has_is_not_delivered_twice(hub, box):
    message = queued()
    box.client.post(message)  # The first post's response was lost; the CLI queued it anyway.
    box.drain_outbox()
    assert len(hub.mac.request("GET", "/v1/inbox?wait=0")["messages"]) == 1


@pytest.fixture
def sending(hub, delivered, monkeypatch):
    """`live send` on this machine (box) with the bridge's hub configured."""
    monkeypatch.setenv("ONE_CONV_HUB_URL", hub.url)
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", hub.box_token)
    monkeypatch.setenv("ONE_CONV_HUB_NODE", "box")
    hubclient.HubClient(hub.url, hub.box_token).request(
        "PUT", "/v1/sessions", {"sessions": [{"address": "omp:worker-1"}]})
    return hub


def test_a_send_to_another_machine_goes_through_the_hub(sending):
    router.send("mac/claude:aaa", "hello mac", SESSION)
    assert sending.mac.request("GET", "/v1/inbox?wait=0")["messages"][0]["text"] == "hello mac"


def test_an_fyi_to_another_machine_is_posted_as_fyi(sending):
    router.send("mac/claude:aaa", "fyi", SESSION, fyi=True)
    assert sending.mac.request("GET", "/v1/inbox?wait=0")["messages"][0]["mode"] == "fyi"


def test_a_send_to_this_machines_own_prefix_is_delivered_here(sending, delivered):
    router.send("box/omp:worker-1", "local after all", None, origin="user")
    assert len(delivered) == 1


def test_a_send_while_the_hub_is_down_is_queued(sending, monkeypatch):
    monkeypatch.setenv("ONE_CONV_HUB_URL", "http://127.0.0.1:9")
    receipt = router.send("mac/claude:aaa", "later", SESSION)
    assert receipt["status"].startswith("queued for the hub")


def test_a_post_cut_off_after_the_hub_stored_it_stays_in_the_outbox(sending, monkeypatch):
    real_post = hubclient.HubClient.post

    def post_then_die(client, message):
        real_post(client, message)
        raise KeyboardInterrupt  # The sender is killed before it reads the hub's answer.
    monkeypatch.setattr(hubclient.HubClient, "post", post_then_die)
    with pytest.raises(KeyboardInterrupt):
        router.send("mac/claude:aaa", "exactly once", SESSION)
    assert len(list(hubclient.outbox().glob("*.json"))) == 1


def test_resuming_a_cut_off_post_does_not_deliver_it_twice(sending, monkeypatch):
    real_post = hubclient.HubClient.post

    def post_then_die(client, message):
        real_post(client, message)
        raise KeyboardInterrupt
    monkeypatch.setattr(hubclient.HubClient, "post", post_then_die)
    with pytest.raises(KeyboardInterrupt):
        router.send("mac/claude:aaa", "exactly once", SESSION)
    monkeypatch.setattr(hubclient.HubClient, "post", real_post)
    bridge.Bridge(hubclient.HubClient(sending.url, sending.box_token, "box"), "box",
                  bridge.Ingress(bridge.bridge_dir() / "ingress.db")).drain_outbox()
    assert len(sending.mac.request("GET", "/v1/inbox?wait=0")["messages"]) == 1


def test_a_posted_send_leaves_nothing_in_the_outbox(sending):
    router.send("mac/claude:aaa", "hello mac", SESSION)
    assert list(hubclient.outbox().glob("*.json")) == []


@pytest.fixture
def stale_forward(hub, delivered, monkeypatch):
    """hub.json naming an ssh forward whose process is gone, and another program now on its port."""
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True,
                              text=True, check=True)
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen()
    squatter.settimeout(0.3)
    bridge._write_private(bridge.bridge_dir() / "hub.json", {
        "url": f"http://127.0.0.1:{squatter.getsockname()[1]}", "node": "box", "token": hub.box_token,
        "tunnel_pid": int(finished.stdout)})
    yield squatter
    squatter.close()


def test_a_send_through_a_dead_forward_is_queued(stale_forward):
    assert router.send("mac/claude:aaa", "secret", SESSION)["status"].startswith("queued for the hub")


def test_a_send_through_a_dead_forward_never_reaches_the_port(stale_forward):
    router.send("mac/claude:aaa", "secret", SESSION)
    with pytest.raises(TimeoutError):
        stale_forward.accept()


def test_a_send_through_the_live_forward_reaches_the_hub(hub, delivered, tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, FAKE_SSH)
    forward = tunnel.Tunnel("hub-box", int(hub.url.rsplit(":", 1)[1]))
    url = forward.open(seconds=10)
    try:
        bridge._write_private(bridge.bridge_dir() / "hub.json", {
            "url": url, "node": "box", "token": hub.box_token, "tunnel_pid": forward.process.pid})
        hubclient.HubClient(url, hub.box_token).request("PUT", "/v1/sessions", {"sessions": [{"address": "omp:worker-1"}]})
        router.send("mac/claude:aaa", "through ssh", SESSION)
    finally:
        forward.close()
    assert hub.mac.request("GET", "/v1/inbox?wait=0")["messages"][0]["text"] == "through ssh"


def test_a_send_to_another_machine_without_a_hub_is_refused(delivered):
    with pytest.raises(router.LiveError, match="needs this machine's bridge"):
        router.send("mac/claude:aaa", "hello", SESSION)


@pytest.mark.parametrize("target, node", [
    ("mac/claude:aaa", "mac"),
    ("boat:sandbox/conversation", None),
    ("mac/nothing", None),
    ("Mac/claude:aaa", None),
    ("claude:aaa", None),
])
def test_remote_address_recognises_only_node_slash_address(target, node):
    assert router.remote_address(target)[0] == node


# ssh ------------------------------------------------------------------------------------

FAKE_SSH = '''#!{python}
"""Stands in for ssh -N -L 127.0.0.1:P:127.0.0.1:H: forwards local port P to H on this machine."""
import socket, sys, threading
spec = sys.argv[sys.argv.index("-L") + 1]
_, port, _, target = spec.split(":")
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", int(port)))
server.listen()
def pipe(a, b):
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        b.close()
while True:
    client, _ = server.accept()
    upstream = socket.create_connection(("127.0.0.1", int(target)))
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
'''


def fake_ssh(tmp_path, monkeypatch, body: str) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "ssh"
    script.write_text(body.replace("{python}", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def test_the_ssh_command_is_fixed_and_non_interactive():
    assert tunnel.command("me@hub-box", 4000, 8789) == [
        "ssh", "-N", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-L", "127.0.0.1:4000:127.0.0.1:8789",
        "--", "me@hub-box"]


@pytest.mark.parametrize("hub", ["ssh://-oProxyCommand=x", "ssh://host;rm", "ssh://a b", "ssh://", "https://x"])
def test_an_unsafe_ssh_destination_is_refused(hub):
    with pytest.raises(tunnel.TunnelError):
        tunnel.destination(hub)


def test_the_hub_answers_through_the_ssh_forward(hub, tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, FAKE_SSH)
    forward = tunnel.Tunnel("hub-box", int(hub.url.rsplit(":", 1)[1]))
    try:
        node = hubclient.HubClient(forward.open(seconds=10), hub.box_token).request("GET", "/v1/whoami")["node"]
    finally:
        forward.close()
    assert node == "box"


def test_an_ssh_that_exits_is_reported(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, "#!/bin/sh\necho 'Host key verification failed.' >&2\nexit 255\n")
    with pytest.raises(tunnel.TunnelError, match="Host key verification failed"):
        tunnel.Tunnel("hub-box", 8789).open(seconds=5)


def test_another_program_on_the_port_is_not_taken_for_the_tunnel(tmp_path, monkeypatch):
    fake_ssh(tmp_path, monkeypatch, "#!/bin/sh\nexec sleep 30\n")
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen()
    monkeypatch.setattr(tunnel, "_free_port", lambda: squatter.getsockname()[1])
    try:
        with pytest.raises(tunnel.TunnelError, match="did not open"):
            tunnel.Tunnel("hub-box", 8789).open(seconds=1.5)
    finally:
        squatter.close()


# serve ----------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def short_poll(monkeypatch):
    """A stop is noticed after the current long-poll, so keep it short here."""
    monkeypatch.setattr(bridge, "POLL_SECONDS", 0.5)

def test_serve_delivers_and_acks_until_stopped(hub, delivered):
    done = threading.Event()
    thread = threading.Thread(target=bridge.serve, args=(hub.url, "box", hub.box_token),
                              kwargs={"stopping": done.is_set}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while "box/omp:worker-1" not in {s["address"] for s in hub.mac.request("GET", "/v1/sessions")["sessions"]}:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    message = post(hub)
    while outcome(hub, message) == "stored":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    done.set()
    thread.join(timeout=15)
    assert outcome(hub, message) == "accepted"


def test_serve_writes_the_hub_config_only_its_user_can_read(hub, delivered):
    done = threading.Event()
    thread = threading.Thread(target=bridge.serve, args=(hub.url, "box", hub.box_token),
                              kwargs={"stopping": done.is_set}, daemon=True)
    thread.start()
    path = bridge.bridge_dir() / "hub.json"
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    done.set()
    thread.join(timeout=15)
    assert (stat.S_IMODE(path.stat().st_mode), json.loads(path.read_text())["node"]) == (0o600, "box")


def test_serve_refuses_a_token_issued_to_another_node(hub, delivered):
    assert bridge.serve(hub.url, "laptop", hub.box_token, stopping=lambda: False) == 1


def test_a_second_bridge_on_the_same_machine_is_refused(hub, delivered):
    held = Path(bridge.bridge_dir() / "serve.lock")
    held.parent.mkdir(parents=True, exist_ok=True)
    with held.open("a") as handle:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert bridge.serve(hub.url, "box", hub.box_token, stopping=lambda: False) == 1
