"""From no running session to one: the hub staffs a request at once, on a connected machine first.

A human request that no running session can take gets a staffing record in the same commit:
a spawn order to a bridge that offers to start sessions, or the cloud when none is connected.
A spawn order that is refused, or that never turns into a claim, moves to the cloud. A claim
closes the record. The Mac's bridge starts the session in tmux; the control service submits
one cloud worker per staffing record.
"""

import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from one_conv.control.runtime import staff_from_cloud
from one_conv.live import bridge, hubclient, spawn
from one_conv.live import hub as hub_module
from one_conv.live.bridge_cli import add_bridge_commands
from one_conv.live.hub import Hub, HubError, application
from tests.test_live_bridge import serve_app

HUMAN = "workspace"


def ask(hub, ident="a", to="#work", text="Fix the build"):
    return hub.send(HUMAN, {"id": ident * 32, "from": "user", "to": to, "text": text, "reply": "expected",
                            "mode": "push"}, join_sender=to.startswith("#"))["task"]


def staffing(hub, key):
    with sqlite3.connect(hub.path) as db:
        return db.execute("SELECT route, state FROM staffing WHERE task = ? ORDER BY version DESC LIMIT 1",
                          (key,)).fetchone()


def spawn_orders(hub, node="mac"):
    return [item for item in hub.inbox(node, 0) if item["to"].startswith("spawn:")]


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    state.publish(HUMAN, [{"address": "user"}])
    state.adopt_trusted_human(HUMAN)
    return state


@pytest.fixture
def spawner(hub):
    """The Mac's bridge is connected, runs no session, and offers to start claude sessions."""
    hub.publish("mac", [], spawn="claude")
    return hub


def test_a_request_with_nobody_anywhere_goes_to_the_cloud(hub):
    assert staffing(hub, ask(hub)["key"]) == ("cloud", "cloud")


def test_a_request_with_a_connected_spawner_goes_to_that_machine(spawner):
    assert staffing(spawner, ask(spawner)["key"]) == ("mac/spawn:claude", "node")


def test_the_spawner_receives_a_spawn_order(spawner):
    ask(spawner)
    assert [item["to"] for item in spawn_orders(spawner)] == ["spawn:claude"]


def test_the_spawn_order_is_an_offer(spawner):
    ask(spawner)
    assert spawn_orders(spawner)[0]["role"] == "candidate"


def test_the_spawn_order_tells_the_session_to_claim_first(spawner):
    task = ask(spawner)
    assert f"one-conv live task claim {task['key']} --version {task['version']}" in spawn_orders(spawner)[0]["text"]


def test_the_spawn_order_carries_the_request(spawner):
    ask(spawner, text="Rename the flag")
    assert "Rename the flag" in spawn_orders(spawner)[0]["text"]


def test_the_spawn_order_is_not_in_the_channel_history(spawner):
    ask(spawner)
    assert [row["text"] for row in spawner.history("work")] == ["Fix the build"]


def test_a_request_with_a_running_member_is_not_staffed(hub):
    hub.publish("mac", [{"address": "claude:one"}], spawn="claude")
    hub.join("mac", "work", "claude:one")
    assert staffing(hub, ask(hub)["key"]) is None


def test_an_offline_spawner_is_skipped(spawner):
    with sqlite3.connect(spawner.path) as db:
        db.execute("UPDATE nodes SET seen = ? WHERE node = 'mac'", (time.time() - hub_module.LEASE_SECONDS - 1,))
    assert staffing(spawner, ask(spawner)["key"]) == ("cloud", "cloud")


def test_a_bridge_that_stops_offering_is_no_longer_a_spawner(spawner):
    spawner.publish("mac", [])
    assert staffing(spawner, ask(spawner)["key"]) == ("cloud", "cloud")


def test_an_unknown_spawn_harness_is_refused(hub):
    with pytest.raises(HubError):
        hub.publish("mac", [], spawn="vim")


def test_a_refused_spawn_moves_to_the_cloud(spawner):
    key = ask(spawner)["key"]
    spawner.ack("mac", spawn_orders(spawner)[0]["delivery"], "refused")
    spawner.sweep()
    assert staffing(spawner, key) == ("cloud", "cloud")


def test_a_spawn_that_never_claims_moves_to_the_cloud(spawner, monkeypatch):
    key = ask(spawner)["key"]
    monkeypatch.setattr(hub_module, "SPAWN_FALLBACK_SECONDS", -1.0)
    spawner.sweep()
    assert staffing(spawner, key) == ("cloud", "cloud")


def test_a_claim_closes_the_staffing(spawner):
    task = ask(spawner)
    spawner.publish("mac", [{"address": "claude:new"}], spawn="claude")
    spawner.claim("mac", {"key": task["key"], "actor": "claude:new", "version": task["version"]})
    spawner.sweep()
    assert staffing(spawner, task["key"]) == ("mac/spawn:claude", "done")


def test_a_spawn_order_handed_out_after_a_claim_is_context(spawner):
    task = ask(spawner)
    spawner.publish("mac", [{"address": "claude:new"}], spawn="claude")
    spawner.claim("mac", {"key": task["key"], "actor": "claude:new", "version": task["version"]})
    assert spawn_orders(spawner)[0]["role"] == "observer"


def test_a_released_request_is_staffed_again(hub, monkeypatch):
    hub.publish("mac", [{"address": "claude:one"}])
    hub.join("mac", "work", "claude:one")
    task = ask(hub)
    hub.claim("mac", {"key": task["key"], "actor": "claude:one", "version": task["version"]})
    monkeypatch.setattr(hub_module, "OWNER_GRACE_SECONDS", -1.0)
    hub.publish("mac", [])
    assert staffing(hub, task["key"]) == ("cloud", "cloud")


def test_an_old_unowned_request_is_not_staffed(hub, monkeypatch):
    monkeypatch.setattr(hub_module, "STAFF_WINDOW_SECONDS", -1.0)
    hub.publish("mac", [{"address": "claude:one"}])
    hub.join("mac", "work", "claude:one")
    task = ask(hub)
    hub.publish("mac", [])
    hub.sweep()
    assert staffing(hub, task["key"]) is None


def test_a_direct_request_is_not_staffed(hub):
    hub.publish("mac", [{"address": "claude:one"}])
    assert staffing(hub, ask(hub, to="mac/claude:one")["key"]) is None


# --- the control service submits cloud workers ---------------------------------------------------


class StubManager:
    """Records submitted worker specs; a spec under a known job id returns that job."""

    def __init__(self):
        self.jobs = {}

    def submit(self, spec, id):
        self.jobs.setdefault(id, {"id": id, "spec": spec})
        return self.jobs[id]


@pytest.fixture
def workers():
    return SimpleNamespace(manager=StubManager(), rate_card=lambda job: None)


def test_the_cloud_staffing_submits_one_worker(hub, workers):
    ask(hub)
    staff_from_cloud(hub, workers)
    assert len(workers.manager.jobs) == 1


def test_the_cloud_worker_joins_the_request_channel(hub, workers):
    ask(hub)
    job, = staff_from_cloud(hub, workers)
    assert job["spec"]["topic"] == "work"


def test_the_cloud_worker_gets_the_claim_instructions(hub, workers):
    task = ask(hub)
    job, = staff_from_cloud(hub, workers)
    assert f"key=\"{task['key']}\"" in job["spec"]["prompt"]


def test_a_submitted_request_is_not_submitted_again(hub, workers):
    ask(hub)
    staff_from_cloud(hub, workers)
    assert staff_from_cloud(hub, workers) == []


def test_the_cloud_staffing_is_marked_submitted(hub, workers):
    key = ask(hub)["key"]
    staff_from_cloud(hub, workers)
    assert staffing(hub, key) == ("cloud", "submitted")


def test_without_cloud_workers_the_request_is_marked_unavailable(hub):
    key = ask(hub)["key"]
    staff_from_cloud(hub, None)
    assert staffing(hub, key) == ("cloud", "unavailable")


# --- the Mac's bridge starts the session --------------------------------------------------------


@pytest.fixture
def started(tmp_path, monkeypatch):
    """Spawn settings for claude in tmp_path; spawn.start calls are recorded instead of run."""
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    calls = []
    monkeypatch.setattr(spawn, "preflight", lambda harness: None)
    monkeypatch.setattr(spawn, "start", lambda *args, **kwargs: calls.append((args, kwargs))
                        or spawn.Spawned("oneconv-claude-1", "claude", str(tmp_path), "claude:new"))
    bridge.save_spawn_settings({"harness": "claude", "cwd": str(tmp_path), "model": None,
                                "args": ["--dangerously-skip-permissions"]})
    return calls


def wait_for(calls):
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.02)
    return calls


def order(role="candidate"):
    return {"to": "spawn:claude", "role": role, "text": "Claim task workspace:x first", "delivery": uuid.uuid4().hex}


def test_a_spawn_order_starts_a_session(started):
    bridge.deliver_inbound(order())
    assert wait_for(started)[0][0][1] == "Claim task workspace:x first"


def test_a_started_session_gets_the_configured_arguments(started):
    bridge.deliver_inbound(order())
    assert wait_for(started)[0][0][4] == ("--dangerously-skip-permissions",)


def test_a_spawn_order_is_accepted(started):
    assert bridge.deliver_inbound(order())[0] == "accepted"


def test_a_spawn_order_after_a_claim_starts_nothing(started):
    bridge.deliver_inbound(order("observer"))
    time.sleep(0.2)
    assert started == []


def test_a_spawn_order_without_settings_is_refused(started):
    bridge.save_spawn_settings(None)
    assert bridge.deliver_inbound(order())[0] == "refused"


def test_a_spawn_order_for_another_harness_is_refused(started):
    assert bridge.deliver_inbound({**order(), "to": "spawn:codex"})[0] == "refused"


def test_a_missing_tmux_refuses_the_spawn_order(started, monkeypatch):
    monkeypatch.setattr(spawn, "preflight", lambda harness: (_ for _ in ()).throw(spawn.SpawnError("no tmux")))
    assert bridge.deliver_inbound(order())[0] == "refused"


def test_the_bridge_offers_to_spawn_over_http(started, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.router, "list_all", lambda harness=None: [])
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    try:
        client = hubclient.HubClient(url, state.add_token("mac"), "mac")
        bridge.Bridge(client, "mac", bridge.Ingress(tmp_path / "ingress.db")).publish()
        state.publish(HUMAN, [{"address": "user"}])
        state.adopt_trusted_human(HUMAN)
        assert staffing(state, ask(state)["key"]) == ("mac/spawn:claude", "node")
    finally:
        stop()


def test_a_bridge_step_turns_the_order_into_a_started_session(started, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.router, "list_all", lambda harness=None: [])
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    try:
        client = hubclient.HubClient(url, state.add_token("mac"), "mac")
        node = bridge.Bridge(client, "mac", bridge.Ingress(tmp_path / "ingress.db"))
        node.publish()
        state.publish(HUMAN, [{"address": "user"}])
        state.adopt_trusted_human(HUMAN)
        ask(state)
        node.step(wait=0)
        assert len(wait_for(started)) == 1
    finally:
        stop()


# --- the spawner settings command ---------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    import click
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    group = click.Group("live")
    add_bridge_commands(group)
    return CliRunner(), group


def test_the_spawner_command_saves_with_yes(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path), "--yes"])
    assert bridge.spawn_settings()["cwd"] == str(tmp_path.resolve())


def test_the_spawner_command_saves_nothing_without_yes(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path)])
    assert bridge.spawn_settings() is None


def test_the_spawner_command_turns_spawning_off(cli, tmp_path):
    runner, group = cli
    runner.invoke(group, ["spawner", "--harness", "claude", "--cwd", str(tmp_path), "--yes"])
    runner.invoke(group, ["spawner", "--off", "--yes"])
    assert bridge.spawn_settings() is None


def test_the_spawner_command_needs_harness_and_cwd_together(cli):
    runner, group = cli
    assert runner.invoke(group, ["spawner", "--harness", "claude", "--yes"]).exit_code != 0
