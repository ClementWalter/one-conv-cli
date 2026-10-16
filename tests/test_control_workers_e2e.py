"""A stub Boat HTTP provider and real loopback hub exercise complete worker job lifecycles."""

import copy
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.control import worker_bootstrap as bootstrap
from one_conv.control.workers import DEFAULT_PROFILE, WorkerManager
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
        return self.call("info", {"box": box})

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
    remote = {"created": 0, "publish": True, "join": True, "verified": True, "sessions": {}, "machine_state": "idle"}

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
                remote["machine_state"] = "idle"
                response = {"id": f"bx_{remote['created']}"}
            elif self.path == "/info":
                response = {"id": body["box"], "state": remote["machine_state"]}
            elif self.path == "/stop" and remote.get("lose_stop"):
                self.send_error(503, "stop outcome is unconfirmed")
                return
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
                remote["machine_state"] = "archived" if self.path == "/stop" else "idle"
                response = {"state": remote["machine_state"]}
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


def test_http_stop_failure_recovers_the_existing_hub_worker(runtime):
    manager, spec, remote = runtime
    manager.submit(spec, "job-1")
    manager.run_once()
    remote["lose_stop"] = True
    pool = manager.list()["pools"][0]["id"]
    manager.stop_pool(pool)
    manager.resume_pool(pool)
    manager.recheck("job-1")
    assert manager.run_once()["status"] == "ready"


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


@pytest.fixture
def invalid_omp_release():
    class Handler(BaseHTTPRequestHandler):
        """A corrupt downloadable release must never become a runnable tool."""

        def log_message(self, format, *args):
            return

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"corrupt release")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/omp"
    server.shutdown()
    thread.join(5)
    server.server_close()


def test_installer_subprocess_rejects_a_corrupt_http_download(tmp_path, invalid_omp_release):
    script = """
import os
from urllib.request import urlopen
from one_conv.control import worker_bootstrap as bootstrap
bootstrap.platform.system = lambda: 'Linux'
bootstrap.platform.machine = lambda: 'x86_64'
bootstrap.OMP_RELEASES = {'x86_64': ('x64', 15, '0' * 64, 62)}
def download(*args, **kwargs):
    response = urlopen(os.environ['RELEASE_URL'], timeout=5)
    response.geturl = lambda: 'https://release-assets.githubusercontent.com/pinned'
    return response
bootstrap.urllib.request.urlopen = download
try:
    bootstrap.install_omp()
except bootstrap.BootstrapError as error:
    raise SystemExit(0 if str(error) == 'omp-checksum' else 2)
raise SystemExit(3)
"""
    result = subprocess.run([sys.executable, "-O", "-c", script], check=False, capture_output=True, text=True,
                            env={**os.environ, "HOME": str(tmp_path), "RELEASE_URL": invalid_omp_release}, timeout=10)
    assert (result.returncode, list((tmp_path / ".local/bin").iterdir())) == (0, [])


@pytest.fixture
def selected_skill_submodule(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "skill-source"
    checkout = tmp_path / "Documents/claudine"
    repository = tmp_path / "Documents/one-conv-cli"
    env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow",
           "GIT_CONFIG_VALUE_0": "always"}

    def git(path, *args):
        return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                               "-C", str(path), *args], capture_output=True, text=True, check=True, env=env)

    source.mkdir()
    target = source / "cli-for-agent/skills/cli-for-agents"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("Selected portable skill.\n")
    git(source, "init", "-q")
    git(source, "add", ".")
    git(source, "commit", "-qm", "fixture")
    checkout.mkdir(parents=True)
    git(checkout, "init", "-q")
    git(checkout, "submodule", "add", "--", str(source), "external/cursor-plugins")
    git(checkout, "submodule", "add", "--", str(source), "external/unselected")
    skills = checkout / ".claude/skills"
    skills.mkdir(parents=True)
    (skills / "cli-for-agents").symlink_to("../../external/cursor-plugins/cli-for-agent/skills/cli-for-agents")
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "fixture")
    git(checkout, "submodule", "deinit", "--force", "--all")
    repository.mkdir()
    git(repository, "init", "-q")
    (repository / "README.md").write_text("Isolated worker checkout.\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "fixture")
    data = {"job_id": "skill-worker", "profile": {"work_repository": "ClementWalter/one-conv-cli",
                                                   "skills": ["cli-for-agents"]}}
    bootstrap.worktree(data, env)
    return checkout, tmp_path / "Documents/workers/skill-worker"


def test_real_git_initializes_the_selected_skill_submodule(selected_skill_submodule):
    _, worktree = selected_skill_submodule
    assert (worktree / ".claude/skills/cli-for-agents/SKILL.md").read_text() == "Selected portable skill.\n"


def test_real_git_preserves_unselected_submodules_as_uninitialized(selected_skill_submodule):
    checkout, _ = selected_skill_submodule
    assert list((checkout / "external/unselected").iterdir()) == []


@pytest.fixture
def real_omp_import(tmp_path, monkeypatch):
    if shutil.which("omp") is None:
        pytest.skip("OMP 18 executable is required for the native import integration")
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    version = subprocess.run(["omp", "--version"], capture_output=True, text=True, check=False, timeout=30, env=env)
    if version.stdout.strip() != "omp/18.0.6":
        pytest.skip("Native import integration requires pinned OMP 18.0.6")
    source = tmp_path / ".pi/agent/auth.json"
    bootstrap.write_private(source, {"anthropic": {"type": "oauth", "access": "synthetic-access-not-a-token",
                                                  "refresh": "synthetic-refresh-not-a-token", "expires": 4102444800000}})
    original = source.read_bytes()
    profile = copy.deepcopy(DEFAULT_PROFILE)
    profile["boat_agent_credentials"] = True
    data = {"spec": {"harness": "omp"}, "profile": profile}
    result = bootstrap.prepare_omp_auth(data, env)
    return result, tmp_path / ".omp/agent/agent.db", source, original, data, env


def test_real_omp_import_accepts_the_selected_synthetic_pi_identity(real_omp_import):
    result, _, _, _, _, _ = real_omp_import
    assert result is True


def test_real_omp_import_creates_an_active_native_oauth_row(real_omp_import):
    _, database, _, _, _, _ = real_omp_import
    assert bootstrap.omp_credential_count(database, active_anthropic=True) == 1


def test_real_omp_import_preserves_the_pi_source(real_omp_import):
    _, _, source, original, _, _ = real_omp_import
    assert source.read_bytes() == original


def test_real_omp_recheck_preserves_a_disabled_native_identity(real_omp_import):
    _, database, _, _, data, env = real_omp_import
    with sqlite3.connect(database) as store:
        store.execute("UPDATE auth_credentials SET disabled_cause='spend_limit'")
    bootstrap.prepare_omp_auth(data, env)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as store:
        cause = store.execute("SELECT disabled_cause FROM auth_credentials").fetchone()[0]
    assert cause == "spend_limit"
