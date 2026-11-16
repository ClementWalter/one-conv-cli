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

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live-hub.py"


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False, timeout=60)


def status(url: str, token: str | None = None) -> int:
    request = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as error:
        return error.code


@pytest.fixture
def relay(tmp_path):
    """The relay script serving on a free loopback port, with a token for node `laptop`."""
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
    status(relay["url"] + "/archive/v1/conversations", relay["token"])
    assert (relay["db"].parent / "archive.db").is_file()


def test_revoking_an_unknown_node_fails(tmp_path):
    assert run("token", "revoke", "ghost", "--db", str(tmp_path / "relay.db")).returncode == 1
