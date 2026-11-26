#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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
#     "numpy==2.5.4",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""`scripts/live-hub.py` as an operator runs it: issue tokens, then serve the relay and its archive in one process."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("sqlalchemy")

from relay_worker import BACKENDS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live-hub.py"
ADMIN_SCRIPT = Path(__file__).resolve().parents[1] / "relay-worker" / "scripts" / "relay_admin.py"


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False, timeout=60)


def run_admin(worker, *args) -> subprocess.CompletedProcess:
    """The Worker relay's token script, as an operator runs it with the admin secret in its environment."""
    return subprocess.run([sys.executable, str(ADMIN_SCRIPT), "--url", worker.url, *args], capture_output=True, text=True,
                          check=False, timeout=60, env={**os.environ, "RELAY_ADMIN_TOKEN": worker.admin})


def status(url: str, token: str | None = None) -> int:
    request = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code


@pytest.fixture(params=BACKENDS)
def relay(request, tmp_path):
    """The relay serving on a free loopback port, with a token for node `laptop` issued by its operator script."""
    if request.param == "worker":
        worker = request.getfixturevalue("relay_worker")
        worker.reset()
        yield {"url": worker.url, "token": run_admin(worker, "token", "add", "laptop").stdout.strip(), "db": None}
        return
    db = tmp_path / "relay.db"
    token = run("token", "add", "laptop", "--db", str(db)).stdout.strip()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([sys.executable, str(SCRIPT), "serve", "--db", str(db), "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            urlopen(url + "/v1/whoami", timeout=1)
        except HTTPError:
            break  # Answering at all (401 without a token) means the server is up.
        except URLError:
            time.sleep(0.1)
    yield {"url": url, "token": token, "db": db}
    process.terminate()
    process.wait(timeout=10)


def test_relay_answers_whoami_with_the_tokens_node(relay):
    request = Request(relay["url"] + "/v1/whoami", headers={"Authorization": f"Bearer {relay['token']}"})
    with urlopen(request, timeout=5) as response:
        assert json.loads(response.read()) == {"node": "laptop"}


def test_relay_archive_refuses_a_request_without_a_token(relay):
    assert status(relay["url"] + "/archive/v1/conversations") == 401


def test_relay_archive_answers_a_node_token(relay):
    assert status(relay["url"] + "/archive/v1/conversations", relay["token"]) == 200


def test_relay_keeps_its_archive_next_to_its_database(relay):
    if relay["db"] is None:
        pytest.skip("the Worker keeps its archive in a Durable Object and R2, not next to a file")
    status(relay["url"] + "/archive/v1/conversations", relay["token"])
    assert (relay["db"].parent / "archive.db").is_file()


def test_revoking_an_unknown_node_fails(tmp_path):
    assert run("token", "revoke", "ghost", "--db", str(tmp_path / "relay.db")).returncode == 1


def test_the_operator_hides_any_board_post(tmp_path):
    from one_conv.live.hub import Hub
    hub = Hub(str(tmp_path / "relay.db"))
    receipt = hub.board.post("mac", "release", {"id": "a" * 32, "from": "user", "text": "oops"})
    run("board-rm", "release", receipt["id"], "--db", str(tmp_path / "relay.db"))
    assert hub.board.read("release", 0, [], 50)["posts"] == []


def test_the_operator_removing_an_unknown_post_fails(tmp_path):
    assert run("board-rm", "release", "a" * 32, "--db", str(tmp_path / "relay.db")).returncode == 1


def test_revoking_an_unknown_node_on_the_worker_fails(relay_worker):
    assert run_admin(relay_worker, "token", "revoke", "ghost").returncode == 1


def test_a_revoked_worker_token_is_refused(relay_worker):
    relay_worker.reset()
    token = run_admin(relay_worker, "token", "add", "laptop").stdout.strip()
    run_admin(relay_worker, "token", "revoke", "laptop")
    assert status(relay_worker.url + "/v1/whoami", token) == 401
