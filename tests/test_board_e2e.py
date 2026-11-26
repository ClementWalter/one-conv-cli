"""The board end to end: two machines (`mac` and `box`, each its own live home) share a topic on a local relay.

mac posts a note with a binary file through the real CLI; box sees one unread post, reads it,
downloads the file byte-identical, and its unread count drops to zero, then rises when mac posts
again. Every command is a separate `one-conv` process, as on two machines.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay_worker import BACKENDS
from test_archive_everywhere import history_env, write_hub
from test_live_hub import serve

pytest.importorskip("uvicorn")

ROOT = Path(__file__).resolve().parents[1]
FILE = bytes(range(256)) * 1000


def one_conv(env: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "one_conv_cli.py"), "board", *args], env=env,
                          cwd=Path(env["ONE_CONV_LIVE_DIR"]).parent,
                          capture_output=True, text=True, timeout=120, check=False)


def machine(root: Path, url: str, node: str, token: str) -> dict:
    """The environment of one machine: its own history, live home and relay settings."""
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("ONE_CONV_HUB")}, **history_env(root),
           "ONE_CONV_NO_UPDATE_CHECK": "1", "ONE_CONV_ARCHIVE_SYNC_MINUTES": "0"}
    root.mkdir(parents=True, exist_ok=True)
    write_hub(env, url, node, token)
    return env


def unread(env: dict) -> int:
    return json.loads(one_conv(env, "topics", "--json").stdout)[0]["unread"]


@pytest.fixture(scope="module", params=BACKENDS)
def scenario(request, tmp_path_factory):
    """Runs the two-machine exchange once per backend and records what each step showed."""
    root = tmp_path_factory.mktemp("board-e2e")
    if request.param == "worker":
        state = request.getfixturevalue("relay_worker").hub()
        url, stop = state.url, lambda: None
    else:
        from one_conv.live.hub import Hub, application
        (root / "relay").mkdir()
        state = Hub(str(root / "relay" / "hub.db"))
        url, stop = serve(application(state))
    mac = machine(root / "mac", url, "mac", state.add_token("mac"))
    box = machine(root / "box", url, "box", state.add_token("box"))
    (root / "mac" / "artifact.bin").write_bytes(FILE)
    seen = SimpleNamespace()
    seen.posted = one_conv(mac, "post", "release", "build artifact for 2.0", "--file", str(root / "mac" / "artifact.bin"),
                           "--tag", "build", "--yes", "--json")
    seen.unread_before = unread(box)
    seen.read = json.loads(one_conv(box, "read", "release", "--json").stdout)
    file_id = seen.read["posts"][0]["files"][0]["id"]
    seen.got = one_conv(box, "get", "release", file_id, "-o", str(root / "box" / "copy.bin"))
    seen.copy = (root / "box" / "copy.bin").read_bytes() if (root / "box" / "copy.bin").exists() else b""
    seen.unread_after_read = unread(box)
    one_conv(mac, "post", "release", "rolled out to staging", "--yes")
    seen.unread_after_new_post = unread(box)
    seen.mac_unread = unread(mac)
    yield seen
    stop()


def test_the_post_succeeds(scenario):
    assert scenario.posted.returncode == 0, scenario.posted.stderr


def test_the_other_machine_sees_one_unread_post(scenario):
    assert scenario.unread_before == 1


def test_the_other_machine_reads_the_post(scenario):
    assert [p["text"] for p in scenario.read["posts"]] == ["build artifact for 2.0"]


def test_the_file_downloads_byte_identical(scenario):
    assert scenario.copy == FILE


def test_reading_clears_the_unread_count(scenario):
    assert scenario.unread_after_read == 0


def test_a_new_post_is_unread_again(scenario):
    assert scenario.unread_after_new_post == 1


def test_the_posting_machine_has_nothing_unread(scenario):
    assert scenario.mac_unread == 0
