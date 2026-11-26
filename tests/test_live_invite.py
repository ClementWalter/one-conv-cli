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
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""`live invite` on a connected machine and `live join` on a new one, against a real local relay.

Each machine is its own live dir (ONE_CONV_LIVE_DIR); launchctl and the `live install` steps
are recorded, never run.
"""

import json
import os
import plistlib
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from click.testing import CliRunner

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.live import bridge, bridge_cli, invite, service
from one_conv.live.cli import build_live_group
from one_conv.live.hub import Hub, application
from one_conv.live.install import Step
from relay_worker import BACKENDS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live-hub.py"
ADMIN_SCRIPT = Path(__file__).resolve().parents[1] / "relay-worker" / "scripts" / "relay_admin.py"


def live(*args, **kwargs):
    return CliRunner().invoke(build_live_group(["one-conv"]), list(args), **kwargs)


def pasted(command: str) -> str:
    """The INVITE argument of a printed `one-conv live join INVITE --yes`, as a shell passes it."""
    return shlex.split(command)[3]


def whoami(url: str, token: str) -> dict:
    request = Request(url + "/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


@pytest.fixture(params=BACKENDS)
def relay(request, tmp_path):
    """(state, URL) of a relay on loopback: the Python relay, or the Worker with ONE_CONV_TEST_RELAY_WORKER=1."""
    if request.param == "worker":
        state = request.getfixturevalue("relay_worker").hub()
        yield state, state.url
        return
    state = Hub(str(tmp_path / "relay.db"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application(state), log_level="error", access_log=False,
                                           timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.02)
    yield state, f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


@pytest.fixture
def machines(relay, tmp_path, monkeypatch):
    """A connected machine `mac` and a fresh one, each a live dir; returns (url, switch(name))."""
    state, url = relay
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "mac"))
    bridge.save_settings(url, "mac", 8789, state.add_token("mac"))

    def switch(name: str) -> None:
        monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / name))
    return url, switch


@pytest.fixture
def recorded(monkeypatch):
    """launchctl on a macOS stand-in and the `live install` steps, recorded instead of run."""
    calls = {"launchctl": [], "applied": []}
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_launchctl", lambda *args: calls["launchctl"].append(list(args))
                        or subprocess.CompletedProcess(args, 113 if args[0] == "print" else 0, "", ""))
    step = Step("claude", "stub step", lambda: bool(calls["applied"]), lambda: calls["applied"].append("claude"))
    monkeypatch.setattr(bridge_cli.installer, "plan", lambda launcher: [step])
    monkeypatch.setattr(bridge_cli, "JOIN_WAIT_SECONDS", 0)
    return calls


@pytest.fixture
def invitation(machines):
    """What `live invite studio` printed on `mac`, then the fresh machine `studio` is current."""
    url, switch = machines
    printed = live("invite", "studio").stdout.strip()
    switch("studio")
    return printed


@pytest.fixture
def joined(machines, invitation, recorded):
    """The fresh machine after pasting the invite with --yes."""
    return live("join", pasted(invitation), "--yes")


# --- the invite string ---------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "seconds"), [("90s", 90), ("15m", 900), ("2h", 7200), ("1d", 86400), ("600", 600)])
def test_a_ttl_reads_seconds_minutes_hours_and_days(text, seconds):
    assert invite.parse_ttl(text) == seconds


def test_a_ttl_without_a_number_is_refused():
    with pytest.raises(invite.InviteError):
        invite.parse_ttl("soon")


def test_an_invite_reads_back_its_hub_node_and_code():
    assert invite.parse_invite("https://relay.example.test#studio.AbCdEf_-0123456789") == (
        "https://relay.example.test", "studio", "AbCdEf_-0123456789")


def test_an_invite_without_a_fragment_is_refused():
    with pytest.raises(invite.InviteError):
        invite.parse_invite("https://relay.example.test")


def test_the_join_command_quotes_the_invite():
    assert invite.join_command("https://relay.example.test", "studio", "AbCdEf_-0123456789") == (
        "one-conv live join 'https://relay.example.test#studio.AbCdEf_-0123456789' --yes")


def test_the_join_command_names_a_non_default_ssh_port():
    assert "--hub-port 2222" in invite.join_command("ssh://me@relay-box", "studio", "AbCdEf_-0123456789", 2222)


# --- live invite ---------------------------------------------------------------------------------

def test_invite_prints_one_join_command(invitation):
    assert invitation.startswith("one-conv live join '") and "\n" not in invitation


def test_invite_binds_the_code_to_the_node(invitation):
    assert invite.parse_invite(pasted(invitation))[1] == "studio"


def test_invite_needs_a_connected_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "fresh"))
    assert "not connected to a relay" in live("invite", "studio").output


def test_invite_refuses_a_node_that_has_a_token(machines):
    assert "already has a token" in live("invite", "mac").output


def test_invite_json_carries_the_command(machines):
    assert json.loads(live("invite", "studio", "--json").stdout)["command"].startswith("one-conv live join ")


# --- live join -----------------------------------------------------------------------------------

def test_join_without_yes_saves_nothing(machines, invitation, recorded):
    live("join", pasted(invitation))
    assert bridge.settings() == {}


def test_join_without_yes_leaves_the_code_unused(machines, invitation, recorded):
    code = pasted(invitation)
    live("join", code)
    assert live("join", code, "--yes").exit_code == 0


def test_join_without_yes_lists_the_login_service(machines, invitation, recorded):
    assert str(service.plist_path()) in live("join", pasted(invitation)).output


def test_join_saves_the_node(joined):
    assert bridge.settings()["node"] == "studio"


def test_join_saves_a_token_the_relay_knows(joined, machines):
    url, _ = machines
    assert whoami(url, bridge.settings()["token"]) == {"node": "studio"}


def test_join_saves_the_settings_privately(joined):
    assert (bridge.bridge_dir() / "serve.json").stat().st_mode & 0o777 == 0o600


def test_join_applies_the_install_steps(joined, recorded):
    assert recorded["applied"] == ["claude"]


def test_join_installs_the_login_service(joined):
    assert plistlib.loads(service.plist_path().read_bytes())["ProgramArguments"][-2:] == ["live", "serve"]


def test_join_loads_the_login_service(joined, recorded):
    assert recorded["launchctl"][-1] == ["bootstrap", service._domain(), str(service.plist_path())]


def test_join_ends_with_a_status_line(joined):
    assert "node studio via http://127.0.0.1:" in joined.output


def test_join_never_prints_the_token(joined):
    assert bridge.settings()["token"] not in joined.output


def test_a_second_join_with_the_same_invite_is_refused(machines, invitation, recorded):
    code = pasted(invitation)
    live("join", code, "--yes")
    assert "already used" in live("join", code, "--yes").output


def test_join_refuses_a_malformed_invite(recorded):
    assert live("join", "https://relay.example.test", "--yes").exit_code == 2


def test_join_on_an_unsupported_system_keeps_the_code(machines, invitation, recorded, monkeypatch):
    code = pasted(invitation)
    monkeypatch.setattr(service.sys, "platform", "win32")
    live("join", code, "--yes")
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert live("join", code, "--yes").exit_code == 0


# --- live install points at the bridge ------------------------------------------------------------

def test_install_points_a_machine_without_a_relay_to_live_join(recorded):
    assert "one-conv live join" in live("install").output


def test_install_says_nothing_of_joining_once_connected(machines, recorded):
    assert "No relay yet" not in live("install").output


# --- operator scripts ----------------------------------------------------------------------------

def test_the_python_relay_operator_invite_redeems(tmp_path):
    db = tmp_path / "relay.db"
    printed = subprocess.run([sys.executable, str(SCRIPT), "invite", "studio", "--db", str(db), "--url",
                              "https://relay.example.test"], capture_output=True, text=True, check=True, timeout=60).stdout
    _, node, code = invite.parse_invite(pasted(printed))
    assert Hub(str(db)).redeem_invite(code)["node"] == node


def test_the_worker_operator_invite_redeems(relay_worker):
    relay_worker.reset()
    printed = subprocess.run([sys.executable, str(ADMIN_SCRIPT), "--url", relay_worker.url, "invite", "studio"],
                             capture_output=True, text=True, check=True, timeout=60,
                             env={**os.environ, "RELAY_ADMIN_TOKEN": relay_worker.admin}).stdout
    _, _, code = invite.parse_invite(pasted(printed))
    assert invite.redeem(relay_worker.url, code)["node"] == "studio"
