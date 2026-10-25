"""Every node's agent sessions are readable by live address from every other node, through the controller."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pydantic")

from starlette.testclient import TestClient

from one_conv import archive, mcp_server
from one_conv.archive import ArchiveClient, ArchiveError
from one_conv.control.runtime import application
from one_conv.live import bridge, router

ROOT = Path(__file__).resolve().parents[1]
SESSION = "worker-session"
ADDRESS = f"worker/claude:{SESSION}"
TEXTS = [f"Quartz step {index}" if index != 4 else
         '<one-conv-message from="laptop/claude:lead" id="msg-42">Check the Quartz build</one-conv-message>'
         for index in range(10)]


def history_env(root: Path) -> dict:
    """Provider homes and one-conv state under `root`, so no test reads the developer's own history."""
    return {"CLAUDE_CONFIG_DIR": str(root / "claude"), "CODEX_HOME": str(root / "codex"),
            "CURSOR_USER_DIR": str(root / "cursor"), "CURSOR_CLI_HOME": str(root / "cursor-cli"),
            "OMP_HOME": str(root / "omp"), "ONE_CONV_CORPUS": str(root / "corpus"),
            "ONE_CONV_CLOUD_CACHE": str(root / "cloud"), "ONE_CONV_CHATGPT_CACHE": str(root / "chatgpt"),
            "AGENT_CONV_CHATGPT_CACHE": str(root / "chatgpt"), "ONE_CONV_STATE_DIR": str(root / "state"),
            "AGENT_CONV_STATE_DIR": str(root / "state"), "ONE_CONV_LIVE_DIR": str(root / "live")}


def write_session(env: dict) -> None:
    """Ten alternating turns, one minute apart from 10:00; turn 4 carries live message msg-42."""
    path = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "-srv-quartz" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    rows = [{"type": "user" if index % 2 == 0 else "assistant", "cwd": "/srv/quartz",
             "timestamp": f"2026-01-01T10:0{index}:00Z",
             "message": {"role": "user" if index % 2 == 0 else "assistant", "content": text}}
            for index, text in enumerate(TEXTS)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def write_hub(env: dict, url: str, node: str, token: str) -> None:
    """The settings a running bridge leaves for the CLI: hub URL, node name and node token."""
    path = Path(env["ONE_CONV_LIVE_DIR"]) / "bridge" / "hub.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "node": node, "token": token}))


@pytest.fixture
def controller(tmp_path):
    """The controller service over real HTTP on loopback, with node tokens for `worker` and `laptop`."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = application(tmp_path / "control", origin)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"origin": origin, "worker": app.state.hub.add_token("worker"), "laptop": app.state.hub.add_token("laptop")}
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


@pytest.fixture
def synced(controller, tmp_path, monkeypatch):
    """The worker's bridge sync has uploaded its history; returns the sync's exit status."""
    env = history_env(tmp_path / "worker")
    write_session(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    sync = bridge.ArchiveSync(minutes=1)
    sync.poll(controller["origin"], controller["worker"], "worker")
    return sync.process.wait(timeout=120)


@pytest.fixture
def laptop(controller, synced, tmp_path):
    """Another machine: no native history, only the bridge settings `live serve` wrote."""
    env = {**os.environ, **history_env(tmp_path / "laptop")}
    write_hub(env, controller["origin"], "laptop", controller["laptop"])
    return env


def one_conv(env, *args):
    return subprocess.run([sys.executable, "-O", str(ROOT / "bin/one-conv"), *args], env=env, text=True,
                          capture_output=True, check=False, timeout=120)


def read_json(env, *args):
    result = one_conv(env, *args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_bridge_sync_completes(synced):
    assert synced == 0


def test_sync_uploads_under_the_node_name(controller, synced):
    client = ArchiveClient(controller["origin"] + "/archive", controller["laptop"])
    assert {row["origin"] for row in client.list_conversations()} == {"worker"}


def test_archive_refuses_an_unknown_token(controller):
    with pytest.raises(ArchiveError, match="401"):
        list(ArchiveClient(controller["origin"] + "/archive", "not-a-node-token").list_conversations())


def test_a_node_cannot_upload_under_another_nodes_name(controller, tmp_path):
    env = {**os.environ, **history_env(tmp_path / "impostor"), "ONE_CONV_ARCHIVE_URL": controller["origin"] + "/archive",
           "ONE_CONV_ARCHIVE_TOKEN": controller["worker"], "ONE_CONV_ARCHIVE_ORIGIN": "laptop"}
    write_session(env)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/archive-sync.py"), "--source", "claude"], env=env,
                            text=True, capture_output=True, check=False, timeout=120)
    assert "HTTP 403" in result.stderr


def test_remote_address_reads_the_other_machines_turns(laptop):
    thread = read_json(laptop, "local", "thread", ADDRESS, "--json", "--no-mark-read")
    assert [turn["text"] for turn in thread["turns"]] == TEXTS


def test_remote_thread_names_its_address(laptop):
    assert read_json(laptop, "local", "thread", ADDRESS, "--json", "--no-mark-read")["address"] == ADDRESS


def test_around_a_message_id_returns_its_neighbours(laptop):
    thread = read_json(laptop, "local", "thread", ADDRESS, "--around", "msg-42", "--context", "1", "--json",
                       "--no-mark-read")
    assert [turn["text"] for turn in thread["turns"]] == TEXTS[3:6]


def test_around_reports_the_anchor_index(laptop):
    thread = read_json(laptop, "local", "thread", ADDRESS, "--around", "msg-42", "--context", "1", "--json",
                       "--no-mark-read")
    assert thread["anchor"] == 1


def test_around_a_time_picks_the_nearest_turn(laptop):
    thread = read_json(laptop, "local", "thread", ADDRESS, "--around", "2026-01-01T10:07:20Z", "--context", "0",
                       "--json", "--no-mark-read")
    assert [turn["text"] for turn in thread["turns"]] == [TEXTS[7]]


def test_search_hits_carry_the_remote_address(laptop):
    hits = read_json(laptop, "local", "search", "Quartz build", "--json")
    assert [hit["address"] for hit in hits] == [ADDRESS]


def test_find_rows_carry_the_remote_address(laptop):
    rows = read_json(laptop, "local", "find", "Quartz step 0", "--json")
    assert [row["address"] for row in rows] == [ADDRESS]


def test_unknown_node_address_is_reported(laptop):
    result = one_conv(laptop, "local", "thread", f"elsewhere/claude:{SESSION}", "--json", "--no-mark-read")
    assert "No conversation elsewhere/claude:worker-session on the archive for node elsewhere" in result.stderr


def test_mcp_reads_a_live_address_around_a_message(laptop, monkeypatch):
    for name, value in laptop.items():
        monkeypatch.setenv(name, value)
    result = mcp_server.read_cli("local", "read", query=ADDRESS, around="msg-42", context=0)
    assert [turn["text"] for turn in result["data"]["turns"]] == [TEXTS[4]]


def test_the_node_reads_its_own_address_from_native_history(controller, synced, tmp_path):
    env = {**os.environ, **history_env(tmp_path / "worker")}
    write_hub(env, controller["origin"], "worker", controller["worker"])
    thread = read_json(env, "local", "thread", ADDRESS, "--json", "--no-mark-read")
    assert thread["turns"][0]["text"] == TEXTS[0]


# Without a hub, a bare harness:id names this machine's own conversation.

@pytest.fixture
def offline(tmp_path):
    env = {**os.environ, **history_env(tmp_path / "solo")}
    write_session(env)
    return env


def test_bare_address_reads_native_history(offline):
    thread = read_json(offline, "local", "thread", f"claude:{SESSION}", "--json", "--no-mark-read")
    assert thread["turns"][-1]["text"] == TEXTS[-1]


def test_bare_address_without_a_hub_has_no_node(offline):
    thread = read_json(offline, "local", "thread", f"claude:{SESSION}", "--json", "--no-mark-read")
    assert thread["address"] == f"claude:{SESSION}"


def test_around_does_not_combine_with_limit(offline):
    result = one_conv(offline, "local", "thread", f"claude:{SESSION}", "--around", "msg-42", "--limit", "3")
    assert "--around picks its own window" in result.stderr


def test_around_text_that_no_turn_holds_is_reported(offline):
    result = one_conv(offline, "local", "thread", f"claude:{SESSION}", "--around", "msg-404", "--no-mark-read")
    assert "No turn contains 'msg-404'" in result.stderr


def test_around_marks_the_anchor_in_text_output(offline):
    result = one_conv(offline, "local", "thread", f"claude:{SESSION}", "--around", "msg-42", "--context", "0",
                      "--no-mark-read")
    assert result.stdout.splitlines()[2].startswith(">> ")


# Archive configuration comes from the live hub settings unless ONE_CONV_ARCHIVE_URL is set.

@pytest.fixture
def hub_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("ONE_CONV_HUB_URL", raising=False)
    monkeypatch.delenv("ONE_CONV_HUB_TOKEN", raising=False)
    monkeypatch.delenv("ONE_CONV_HUB_NODE", raising=False)
    monkeypatch.delenv("ONE_CONV_ARCHIVE_ORIGIN", raising=False)
    write_hub({"ONE_CONV_LIVE_DIR": os.environ["ONE_CONV_LIVE_DIR"]}, "https://hub.example.test", "macbook", "node-token")


def test_hub_settings_configure_the_archive_url(hub_settings):
    assert archive.configured_url() == "https://hub.example.test/archive"


def test_hub_settings_configure_the_upload_origin(hub_settings):
    assert archive.configured_origin() == "macbook"


def test_hub_settings_configure_the_node_token(hub_settings):
    assert archive.configured_client().token == "node-token"


def test_an_empty_archive_url_turns_the_archive_off(hub_settings, monkeypatch):
    monkeypatch.setenv("ONE_CONV_ARCHIVE_URL", "")
    assert archive.configured_client() is None


def test_an_explicit_archive_keeps_its_installation_origin(hub_settings, monkeypatch):
    monkeypatch.setenv("ONE_CONV_ARCHIVE_URL", "https://archive.example.test")
    assert archive.configured_origin() is None


def test_no_hub_means_no_archive():
    assert archive.configured_url() is None


# The controller mounts the archive behind the hub's node tokens.

@pytest.fixture
def control_app(tmp_path):
    app = application(tmp_path / "control", "https://workspace.example.test")
    with TestClient(app, base_url="https://workspace.example.test") as client:
        yield app, client


def test_controller_archive_requires_a_node_token(control_app):
    _, client = control_app
    assert client.get("/archive/v1/conversations").status_code == 401


def test_controller_archive_lists_for_any_node_token(control_app):
    app, client = control_app
    token = app.state.hub.add_token("laptop")
    response = client.get("/archive/v1/conversations", headers={"Authorization": "Bearer " + token})
    assert response.json() == {"items": [], "next": None}


# The bridge runs the sync in the background with the node's own credentials.

@pytest.fixture
def recorder(tmp_path):
    """A stand-in sync script that records the arguments and archive settings it was started with."""
    script = tmp_path / "record.py"
    output = tmp_path / "recorded.json"
    script.write_text("import json, os, sys\n"
                      f"open({str(output)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], "
                      "'env': {k: v for k, v in os.environ.items() if k.startswith('ONE_CONV_ARCHIVE')}}))\n")
    return script, output


@pytest.fixture
def recorded(recorder):
    script, output = recorder
    sync = bridge.ArchiveSync(minutes=5, script=script)
    sync.poll("https://hub.example.test", "node-token", "macbook")
    sync.process.wait(timeout=30)
    return json.loads(output.read_text())


def test_bridge_sync_targets_the_hub_archive(recorded):
    assert recorded["env"]["ONE_CONV_ARCHIVE_URL"] == "https://hub.example.test/archive"


def test_bridge_sync_uploads_under_the_node_name(recorded):
    assert recorded["env"]["ONE_CONV_ARCHIVE_ORIGIN"] == "macbook"


def test_bridge_sync_keeps_the_token_out_of_argv(recorded):
    assert "node-token" not in json.dumps(recorded["argv"])


@pytest.mark.parametrize("source", bridge.ARCHIVE_SOURCES)
def test_bridge_sync_covers_each_agent_harness(recorded, source):
    assert source in recorded["argv"]


def test_bridge_sync_waits_for_its_interval(recorder):
    sync = bridge.ArchiveSync(minutes=5, script=recorder[0])
    sync.poll("https://hub.example.test", "node-token", "macbook")
    sync.process.wait(timeout=30)
    assert sync.poll("https://hub.example.test", "node-token", "macbook") is False


@pytest.fixture
def lanes_recorded(tmp_path):
    """Both lanes started once with a stand-in script that records each run's arguments."""
    script = tmp_path / "record.py"
    script.write_text("import json, os, sys, uuid\n"
                      f"open(os.path.join({str(tmp_path)!r}, uuid.uuid4().hex + '.run'), 'w').write(json.dumps(sys.argv[1:]))\n")
    lanes = bridge.ArchiveLanes(minutes=1, script=script)
    lanes.poll("https://hub.example.test", "node-token", "macbook")
    for lane in lanes.lanes:
        lane.process.wait(timeout=30)
    return [json.loads(path.read_text()) for path in tmp_path.glob("*.run")]


def test_the_agent_lane_leaves_out_the_cursor_ide(lanes_recorded):
    agents, = [argv for argv in lanes_recorded if "--lane" not in argv]
    assert "cursor" not in agents


def test_the_ide_lane_runs_with_its_own_lane_name(lanes_recorded):
    ide, = [argv for argv in lanes_recorded if "--lane" in argv]
    assert ide == ["--source", "cursor", "--lane", "ide"]


def test_the_ide_lane_runs_less_often():
    agents, ide = bridge.ArchiveLanes(minutes=1).lanes
    assert ide.seconds == bridge.IDE_SYNC_MINUTES * 60


def test_bridge_sync_turned_off_never_starts(recorder):
    assert bridge.ArchiveSync(minutes=0, script=recorder[0]).poll("https://hub.example.test", "t", "macbook") is False


class StubSync:
    """Records what the bridge asks of its archive sync."""

    def __init__(self):
        self.polls, self.stopped = [], False

    def poll(self, url, token, node):
        self.polls.append((url, token, node))
        return True

    def stop(self):
        self.stopped = True


@pytest.fixture
def served(controller, monkeypatch):
    monkeypatch.setattr(router, "list_all", lambda harness=None: [])
    # One short inbox poll is enough to reach the sync; the prompt mirror is another test's subject.
    monkeypatch.setattr(bridge, "POLL_SECONDS", 0.2)
    stub = StubSync()
    bridge.serve(controller["origin"], "laptop", controller["laptop"], stopping=lambda: bool(stub.polls),
                 archive=stub, prompts=StubSync())
    return controller, stub


def test_bridge_serve_syncs_with_its_hub_and_node(served):
    controller, stub = served
    assert stub.polls[0] == (controller["origin"], controller["laptop"], "laptop")


def test_bridge_serve_stops_its_sync_on_exit(served):
    assert served[1].stopped is True
