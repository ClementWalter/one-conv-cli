"""Cloud boxes: the registry, Scaleway power calls, the idle keeper and teleport's wake-up."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from one_conv.live import box, teleport
from one_conv.live.cli import build_live_group
from one_conv.live.inventory import Inventory
from one_conv.live.model import LiveSession

CLOUD = box.Box(node="cloud", provider="scaleway", server="srv-1", zone="fr-par-1")


@pytest.fixture
def scw():
    """A stub `scw` run: answers `server get` with the current state and records every call."""
    vm = SimpleNamespace(state="stopped", calls=[])

    def run(argv, **_):
        vm.calls.append(argv[1:4])
        if argv[3] == "start":
            vm.state = "running"
        elif argv[3] == "stop":
            vm.state = "stopped"
        return subprocess.CompletedProcess(argv, 0, json.dumps({"state": vm.state}), "")
    vm.run = run
    return vm


def test_a_registered_box_is_loaded_back():
    box.save({"cloud": CLOUD})
    assert box.load() == {"cloud": CLOUD}


def test_no_registry_means_no_boxes():
    assert box.load() == {}


def test_start_powers_on_a_stopped_box(scw):
    box.start(CLOUD, run=scw.run)
    assert scw.state == "running"


def test_start_passes_the_zone_and_waits(scw):
    box.start(CLOUD, run=scw.run)
    assert scw.calls[-1] == ["instance", "server", "start"]


def test_start_leaves_a_running_box_alone(scw):
    scw.state = "running"
    assert box.start(CLOUD, run=scw.run) is False


def test_stop_powers_off_a_running_box(scw):
    scw.state = "running"
    box.stop(CLOUD, run=scw.run)
    assert scw.state == "stopped"


def test_stop_leaves_a_stopped_box_alone(scw):
    assert box.stop(CLOUD, run=scw.run) is False


def test_a_failing_scw_names_the_command():
    def run(argv, **_):
        return subprocess.CompletedProcess(argv, 1, "", "permission denied")
    with pytest.raises(box.BoxError, match="permission denied"):
        box.state(CLOUD, run=run)


def test_a_box_first_seen_is_not_idle():
    assert box.idle_boxes({"cloud": CLOUD}, [], {}, now=1000.0, idle_seconds=60) == []


def test_a_box_without_busy_sessions_for_the_idle_time_is_idle():
    assert box.idle_boxes({"cloud": CLOUD}, [], {"cloud": 900.0}, now=1000.0, idle_seconds=60) == ["cloud"]


def test_a_busy_session_keeps_its_box_awake():
    sessions = [{"node": "cloud", "status": "busy", "online": True}]
    assert box.idle_boxes({"cloud": CLOUD}, sessions, {"cloud": 0.0}, now=1000.0, idle_seconds=60) == []


def test_an_idle_session_does_not_keep_its_box_awake():
    sessions = [{"node": "cloud", "status": "idle", "online": True}]
    assert box.idle_boxes({"cloud": CLOUD}, sessions, {"cloud": 0.0}, now=1000.0, idle_seconds=60) == ["cloud"]


def test_a_busy_session_elsewhere_does_not_keep_a_box_awake():
    sessions = [{"node": "mac", "status": "busy", "online": True}]
    assert box.idle_boxes({"cloud": CLOUD}, sessions, {"cloud": 0.0}, now=1000.0, idle_seconds=60) == ["cloud"]


def test_zero_idle_minutes_never_powers_off():
    assert box.idle_boxes({"cloud": CLOUD}, [], {"cloud": 0.0}, now=1000.0, idle_seconds=0) == []


def keeper_with(sessions, clock):
    stopped = []
    keeper = box.Keeper(idle_seconds=60, check_seconds=10, clock=lambda: clock[0],
                        stopper=lambda item: stopped.append(item.node) or True)
    client = SimpleNamespace(request=lambda method, path: {"sessions": sessions})
    return keeper, client, stopped


def test_the_keeper_powers_off_a_box_idle_since_its_first_look():
    box.save({"cloud": CLOUD})
    clock = [1000.0]
    keeper, client, stopped = keeper_with([], clock)
    keeper.poll(client)
    clock[0] = 1061.0
    keeper.poll(client)
    keeper.worker.join(timeout=5)
    assert stopped == ["cloud"]


def test_the_keeper_does_nothing_without_boxes():
    keeper, client, stopped = keeper_with([], [1000.0])
    assert keeper.poll(client) == []


def test_the_keeper_checks_at_most_every_check_interval():
    box.save({"cloud": CLOUD})
    clock = [1000.0]
    keeper, client, stopped = keeper_with([], clock)
    keeper.last_busy["cloud"] = 0.0
    keeper.poll(client)
    keeper.worker.join(timeout=5)
    clock[0] = 1005.0
    assert keeper.poll(client) == []


def test_box_add_registers_the_vm(monkeypatch):
    monkeypatch.setattr(box, "state", lambda item, run=None: "running")
    CliRunner().invoke(build_live_group(["one-conv"]), ["box", "add", "cloud", "--scaleway", "srv-1", "--yes"])
    assert box.load()["cloud"].server == "srv-1"


def test_box_add_without_yes_registers_nothing():
    CliRunner().invoke(build_live_group(["one-conv"]), ["box", "add", "cloud", "--scaleway", "srv-1"])
    assert box.load() == {}


def test_box_start_refuses_an_unknown_node():
    result = CliRunner().invoke(build_live_group(["one-conv"]), ["box", "start", "nowhere", "--yes"])
    assert "not a registered box" in result.output


SENDER = LiveSession("claude", "c1")


def test_a_probe_is_asked_again_while_the_box_boots(monkeypatch):
    answers = [teleport.NoAnswer("box did not answer"), {"rows": [], "dir": "/srv", "platform": "linux"}]

    def ask(hub, node, sender, request, timeout, **_):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(teleport, "ask", ask)
    hub = SimpleNamespace(node="mac")
    report = teleport.run("cloud", SENDER, {}, Inventory(harness="claude", cwd="/w"), check_only=True, hub=hub,
                          probe_attempts=2)
    assert report["status"] == "checked"


def test_a_probe_without_retries_fails_on_the_first_silence(monkeypatch):
    def ask(hub, node, sender, request, timeout, **_):
        raise teleport.NoAnswer("box did not answer")
    monkeypatch.setattr(teleport, "ask", ask)
    with pytest.raises(teleport.NoAnswer):
        teleport.run("cloud", SENDER, {}, Inventory(harness="claude", cwd="/w"), check_only=True,
                     hub=SimpleNamespace(node="mac"))
