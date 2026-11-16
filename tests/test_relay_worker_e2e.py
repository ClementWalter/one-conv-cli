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
"""Two real bridges (`one-conv live serve`) on the Worker relay: a cross-node send and a cross-node read.

Node `box` runs a stand-in omp session (a registry file naming a `sleep` process; delivery drops a
file in its inbox) and one Claude transcript its bridge syncs to the archive. Node `mac` sends to
the stand-in and reads the transcript by its live address. Every path is under the test's own
directory, so no real session is listed, messaged or read. Runs only with ONE_CONV_TEST_RELAY_WORKER=1.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from test_archive_everywhere import SESSION, TEXTS, history_env, write_session

ROOT = Path(__file__).resolve().parents[1]
STUB = "e2e-stub"
DEADLINE = 90


def cli(env: dict, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "one_conv_cli.py"), *args], env=env, input=stdin, text=True,
                          capture_output=True, check=False, timeout=120)


def node_env(root: Path, sync_minutes: str) -> dict:
    """A machine's environment: its own histories and live home, no relay or archive inherited from the caller."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("ONE_CONV_HUB_", "ONE_CONV_ARCHIVE_"))}
    return {**env, **history_env(root), "ONE_CONV_ARCHIVE_SYNC_MINUTES": sync_minutes}


def published(worker, token: str) -> set[str]:
    """Addresses in the relay's directory."""
    request = Request(worker.url + "/v1/sessions", headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=10) as response:
        return {row["address"] for row in json.loads(response.read())["sessions"]}


def outcome(env: dict, message_id: str) -> str | None:
    """The first recipient's outcome as `live status` reports it, once it is past `stored`."""
    result = cli(env, "live", "status", message_id, "--json")
    found = json.loads(result.stdout)["recipients"][0]["outcome"] if result.returncode == 0 else None
    return None if found == "stored" else found


def until(check, what: str):
    """The first truthy result of `check()`, polled until DEADLINE."""
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def nodes(relay_worker, tmp_path_factory):
    """Bridges for `mac` and `box` running against the Worker; box has synced its transcript."""
    relay_worker.reset()
    root = tmp_path_factory.mktemp("e2e")
    mac, box = node_env(root / "mac", "0"), node_env(root / "box", "1")
    write_session(box)
    stub = subprocess.Popen(["sleep", "600"])
    registry = Path(box["ONE_CONV_LIVE_DIR"]) / "omp" / f"{STUB}.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"sessionId": STUB, "pid": stub.pid, "cwd": "/srv/stub", "title": "e2e stub"}))
    bridges, tokens = [], {}
    for name, env in (("mac", mac), ("box", box)):
        token = tokens[name] = relay_worker.admin_call("POST", "/admin/tokens", {"node": name})[1]["token"]
        process = subprocess.Popen([sys.executable, str(ROOT / "one_conv_cli.py"), "live", "serve", "--hub", relay_worker.url,
                                    "--node", name, "--token-stdin"], env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=(root / f"{name}-bridge.log").open("w"), text=True)
        process.stdin.write(token)
        process.stdin.close()
        bridges.append(process)
    try:
        until(lambda: f"box/omp:{STUB}" in published(relay_worker, tokens["mac"]), "box's stand-in session")
        until(lambda: any(row.get("address") == f"box/claude:{SESSION}" for row in
                          json.loads(cli(mac, "local", "find", "Quartz step 0", "--json").stdout or "[]")), "box's archive sync")
        yield {"mac": mac, "box": box}
    finally:
        for process in bridges:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)
        stub.kill()
        stub.wait()


@pytest.fixture(scope="module")
def sent(nodes):
    """mac sends one message to box's stand-in session; returns the inbox file the box bridge wrote."""
    result = cli(nodes["mac"], "live", "send", f"box/omp:{STUB}", "hello from mac", "--yes", "--json")
    assert result.returncode == 0, result.stderr
    inbox = Path(nodes["box"]["ONE_CONV_LIVE_DIR"]) / "inbox" / "omp" / STUB
    return until(lambda: sorted(inbox.glob("*.json")), "the delivery on box")[0], json.loads(result.stdout)


def test_a_cross_node_send_reaches_the_stand_in_session(sent):
    path, _ = sent
    assert "hello from mac" in json.loads(path.read_text())["text"]


def test_the_sender_reads_the_accepted_outcome(nodes, sent):
    _, receipt = sent
    assert until(lambda: outcome(nodes["mac"], receipt["id"]), "the outcome") == "accepted"


def test_the_other_node_reads_the_synced_transcript(nodes):
    thread = json.loads(cli(nodes["mac"], "local", "thread", f"box/claude:{SESSION}", "--json", "--no-mark-read").stdout)
    assert [turn["text"] for turn in thread["turns"]] == TEXTS
