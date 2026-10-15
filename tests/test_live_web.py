"""The read-only live viewer over real HTTP, and the message log it reads."""

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.live import router, team, web
from one_conv.live.model import LiveSession


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("omp", "OMP_HOME"),
                      ("cursor", "ONE_CONV_CURSOR_HOME"), ("live", "ONE_CONV_LIVE_DIR")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _omp(homes, session_id):
    (homes / "live" / "omp").mkdir(exist_ok=True)
    (homes / "live" / "omp" / f"{session_id}.json").write_text(
        json.dumps({"pid": os.getppid(), "sessionId": session_id, "cwd": f"/work/{session_id}"}))


@pytest.fixture
def viewer(homes):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web.application(port), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.02)
    yield port
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


def fetch(port, path, method="GET", host=None):
    """(status, headers, body) of one request, with an explicit Host header."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = connection.getresponse()
    return response.status, dict(response.getheaders()), response.read()


# --- message log ---------------------------------------------------------------------------


def test_send_logs_the_message_text(homes):
    _omp(homes, "o1")
    router.send("omp:o1", "please review", LiveSession("claude", "c1"))
    assert web.log()[-1]["text"] == "please review"


def test_topic_send_is_logged_once_with_per_member_status(homes):
    for session in ("o1", "o2"):
        _omp(homes, session)
        team.join("release", f"omp:{session}")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert sorted(web.log()[-1]["status"]) == ["omp:o1", "omp:o2"]


def test_log_keeps_only_the_latest_messages(homes, monkeypatch):
    monkeypatch.setattr(router, "MESSAGE_LOG", 3)
    for index in range(5):
        router.log_message({"id": str(index), "text": f"m{index}"})
    assert [row["id"] for row in web.log()] == ["2", "3", "4"]


def test_log_failure_does_not_fail_the_send(homes, monkeypatch):
    _omp(homes, "o1")

    def broken(row):
        raise OSError("disk full")

    monkeypatch.setattr(router, "log_message", broken)
    assert router.send("omp:o1", "hi", LiveSession("claude", "c1"))["status"] == "delivered"


def test_direct_view_groups_messages_by_pair(homes):
    _omp(homes, "o1")
    router.send("omp:o1", "one", LiveSession("claude", "c1"))
    router.send("omp:o1", "two", LiveSession("claude", "c1"))
    assert [len(t["messages"]) for t in web.direct()["threads"]] == [2]


def test_direct_view_leaves_out_topic_messages(homes):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert web.direct()["threads"] == []


def test_corrupt_log_line_does_not_hide_the_rest(homes):
    router.log_message({"id": "a", "text": "kept"})
    with (homes / "live" / "messages.jsonl").open("a") as stream:
        stream.write("{not json\n")
    assert [row["text"] for row in web.log()] == ["kept"]


# --- HTTP ------------------------------------------------------------------------------------


def test_state_lists_topics(viewer, homes):
    team.join("release", "omp:o1")
    _, _, body = fetch(viewer, "/api/state")
    assert [t["topic"] for t in json.loads(body)["topics"]] == ["release"]


def test_topic_endpoint_returns_its_history(viewer, homes):
    router.send("#release", "first", LiveSession("claude", "c1"))
    _, _, body = fetch(viewer, "/api/topics/release")
    assert [m["text"] for m in json.loads(body)["messages"]] == ["first"]


def test_viewing_a_topic_does_not_move_any_readers_position(viewer, homes):
    team.join("release", "omp:o1")
    router.send("#release", "first", LiveSession("claude", "c1"))
    fetch(viewer, "/api/topics/release")
    assert [m["text"] for m in team.catch_up("release", "omp:o1")["messages"]] == ["first"]


def test_foreign_host_header_is_refused(viewer):
    status, _, _ = fetch(viewer, "/api/state", host="attacker.example")
    assert status == 421


def test_writes_are_not_accepted(viewer):
    status, _, _ = fetch(viewer, "/api/state", method="POST")
    assert status == 405


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/state"])
def test_responses_carry_a_strict_content_policy(viewer, path):
    _, headers, _ = fetch(viewer, path)
    assert headers["content-security-policy"].startswith("default-src 'none'; script-src 'self'")


def test_invalid_topic_is_a_client_error(viewer):
    status, _, _ = fetch(viewer, "/api/topics/bad%20topic")
    assert status == 400


def test_topic_with_a_slash_matches_no_route(viewer):
    status, _, _ = fetch(viewer, "/api/topics/..%2Fetc")
    assert status == 404


def test_page_script_never_writes_html():
    assert "innerHTML" not in web.SCRIPT and "outerHTML" not in web.SCRIPT and "insertAdjacentHTML" not in web.SCRIPT


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_script_parses(tmp_path):
    script = tmp_path / "app.js"
    script.write_text(web.SCRIPT)
    assert subprocess.run(["node", "--check", str(script)], capture_output=True, check=False).returncode == 0
