"""A stub Boat HTTP provider and real loopback hub exercise complete worker job lifecycles."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.control import worker_bootstrap as bootstrap
from one_conv.control.workers import WorkerManager
from one_conv.live.hub import Hub, application
from one_conv.live.hubclient import HubClient


class StubBoatHTTP:
    """The provisioning boundary uses real HTTP while the native runtime is a deterministic stub."""

    def __init__(self, url):
        self.url = url

    def call(self, operation, payload):
        request = Request(self.url + "/" + operation, json.dumps(payload).encode(),
                          {"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def credentials(self, profile):
        return self.call("credentials", {})

    def create(self, profile, pool_id):
        return self.call("create", {"pool_id": pool_id})["id"]

    def bootstrap(self, box, payload):
        return self.call("bootstrap", {"box": box, "payload": payload})

    def info(self, box):
        return {"id": box}

    def stop_pool(self, box):
        return self.call("stop", {"box": box})

    def resume_pool(self, box):
        return self.call("resume", {"box": box})


@pytest.fixture
def loopback_hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application(state), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.01)
    yield state, f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    thread.join(5)
    sock.close()


@pytest.fixture
def provider(loopback_hub):
    remote = {"created": 0, "publish": True, "join": True, "verified": True, "sessions": {}}

    class Handler(BaseHTTPRequestHandler):
        """Only fixed test health is returned; request bodies stay out of server logs."""

        def log_message(self, format, *args):
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/credentials":
                response = {"service_account": "test-service-account"}
            elif self.path == "/create":
                remote["created"] += 1
                response = {"id": f"bx_{remote['created']}"}
            elif self.path == "/bootstrap":
                data = body["payload"]
                if data["operation"] == "stop":
                    response = {"status": "stopped"}
                else:
                    address = f"{data['spec']['harness']}:{data['job_id']}"
                    client = HubClient(data["hub"]["url"], data["hub"]["token"], data["hub"]["node"])
                    sessions = remote["sessions"].setdefault(data["pool_id"], {})
                    sessions[address] = {"address": address, "name": data["spec"]["name"], "status": "idle"}
                    if remote["publish"]:
                        client.request("PUT", "/v1/sessions", {"sessions": list(sessions.values())})
                    if remote["join"] and remote["publish"]:
                        client.join(data["spec"]["topic"], address)
                    response = {"status": "ready", "address": address, "authenticated": True, "registered": True,
                                "model_verified": remote["verified"], "bridge": True, "published": True, "joined": True,
                                "collector": True, "task_id": "a" * 32}
            else:
                response = {"status": "stopped" if self.path == "/stop" else "running"}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield remote, StubBoatHTTP(f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    thread.join(5)
    server.server_close()


@pytest.fixture
def runtime(tmp_path, loopback_hub, provider):
    state, hub_url = loopback_hub
    remote, transport = provider
    manager = WorkerManager(tmp_path / "workers", {"public_hub_url": hub_url, "hub": state, "boat": transport})
    spec = {"name": "reviewer", "harness": "claude", "model": "selected-model", "topic": "release",
            "profile": "default", "prompt": "Review this repository."}
    return manager, spec, remote


def test_a_worker_is_ready_only_after_real_hub_publication_and_membership(runtime):
    manager, spec, _ = runtime
    manager.submit(spec, "job-1")
    assert manager.run_once()["status"] == "ready"


def test_public_job_address_routes_through_its_remote_node(runtime):
    manager, spec, _ = runtime
    manager.submit(spec, "job-1")
    job = manager.run_once()
    pool = manager.list()["pools"][0]
    assert job["address"] == f"{pool['node']}/claude:job-1"


def test_a_claimed_publication_without_a_hub_entry_is_unconfirmed(runtime):
    manager, spec, remote = runtime
    remote["publish"] = False
    manager.submit(spec, "job-1")
    assert manager.run_once()["status"] == "unconfirmed"


def test_a_missing_topic_membership_cannot_claim_readiness(runtime):
    manager, spec, remote = runtime
    remote["join"] = False
    manager.submit(spec, "job-1")
    assert manager.run_once()["status"] == "unconfirmed"


def test_a_pid_and_hub_entry_do_not_replace_a_verified_model_answer(runtime):
    manager, spec, remote = runtime
    remote["verified"] = False
    manager.submit(spec, "job-1")
    assert manager.run_once()["status"] == "unconfirmed"


def test_two_native_harnesses_share_one_box_and_one_hub_node(runtime, loopback_hub):
    manager, spec, _ = runtime
    manager.submit(spec, "job-1")
    manager.run_once()
    manager.submit({**spec, "harness": "codex", "name": "builder"}, "job-2")
    manager.run_once()
    state, _ = loopback_hub
    assert len({row["node"] for row in state.directory()}) == 1


def test_stopping_then_reusing_a_cold_pool_resumes_the_tracked_box(runtime):
    manager, spec, remote = runtime
    manager.submit(spec, "job-1")
    manager.run_once()
    manager.stop_pool(manager.list()["pools"][0]["id"])
    manager.submit({**spec, "name": "builder"}, "job-2")
    manager.run_once()
    assert remote["created"] == 1


@pytest.fixture
def task_dispatch(loopback_hub, tmp_path):
    state, url = loopback_hub
    token = state.add_token("worker-node")
    client = HubClient(url, token, "worker-node")
    client.request("PUT", "/v1/sessions", {"sessions": [{"address": "claude:resident", "name": "reviewer"}]})
    client.join("release", "claude:resident")
    data = {"pool_id": "pool-1", "job_id": "job-1", "hub": {"url": url, "token": token, "node": "worker-node"},
            "spec": {"name": "reviewer", "topic": "release", "prompt": "Continue the authorized review."}}
    record = {"request_hash": "known-request"}
    path = tmp_path / "worker.json"
    return data, path, record, client


def test_the_initial_task_is_durably_accepted_through_the_real_hub(task_dispatch):
    data, path, record, client = task_dispatch
    bootstrap._dispatch_task(data, "claude:resident", path, record)
    assert len(client.request("GET", "/v1/inbox?wait=0")["messages"]) == 1


def test_retrying_task_dispatch_does_not_post_a_second_task(task_dispatch):
    data, path, record, client = task_dispatch
    bootstrap._dispatch_task(data, "claude:resident", path, record)
    bootstrap._dispatch_task(data, "claude:resident", path, record)
    assert len(client.request("GET", "/v1/inbox?wait=0")["messages"]) == 1


def test_task_relay_does_not_forge_a_new_human_approval(task_dispatch):
    data, path, record, client = task_dispatch
    bootstrap._dispatch_task(data, "claude:resident", path, record)
    assert client.request("GET", "/v1/inbox?wait=0")["messages"][0]["from"] == "worker-node/unknown"


def test_hub_configuration_exists_before_any_native_process_starts(loopback_hub, monkeypatch):
    from one_conv.live import bridge, hubclient

    state, url = loopback_hub
    token = state.add_token("worker-node")
    data = {"hub": {"url": url, "token": token, "node": "worker-node"}}
    monkeypatch.delenv("ONE_CONV_HUB_URL", raising=False)
    monkeypatch.delenv("ONE_CONV_HUB_TOKEN", raising=False)
    monkeypatch.delenv("ONE_CONV_HUB_NODE", raising=False)
    monkeypatch.setattr(bridge, "status", lambda: {"running": True})
    bootstrap._bridge(data, ["one-conv"])
    assert hubclient.configured().node == "worker-node"
