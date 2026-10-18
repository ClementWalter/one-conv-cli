"""Task delivery on the agent side: envelopes per role, the task client, and local no-hub requests."""

import json

import pytest
from click.testing import CliRunner

from one_conv.live import hubclient, router, taskops, tasks, team
from one_conv.live.cli import build_live_group
from one_conv.live.model import LiveSession, envelope

KEY = "workspace:" + "a" * 32
TASK = {"key": KEY, "version": 2, "owner": "mac/claude:lead"}


@pytest.mark.parametrize("role, phrase", [
    ("owner", "You own this request"),
    ("collaborator", "Work only this part"),
    ("observer", "Context only"),
])
def test_the_envelope_tells_each_role_what_it_may_do(role, phrase):
    assert phrase in envelope("Book the train", None, "workspace/user", "expected", "m1", "general", None, TASK, role)


def test_an_observer_is_not_told_to_reply():
    wrapped = envelope("Book the train", None, "workspace/user", "none", "m1", "general", None, TASK, "observer")
    assert "send_message" not in wrapped


def test_the_envelope_carries_the_task_version():
    wrapped = envelope("Book the train", None, "workspace/user", "expected", "m1", "general", None, TASK, "owner")
    assert 'task-version="2"' in wrapped


class LostAnswer:
    """A hub that applies the action, then loses the answer on the way back."""

    node = "mac"
    applied = 0

    def task_action(self, action, body):
        LostAnswer.applied += 1
        raise hubclient.HubUnavailable("response lost")


@pytest.fixture
def lost(monkeypatch, tmp_path):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path))
    monkeypatch.setattr(hubclient, "configured", lambda: LostAnswer())
    with pytest.raises(taskops.TaskOpError) as refused:
        taskops.claim(KEY, "claude:lead")
    return refused.value


def test_a_lost_answer_is_reported_as_unconfirmed(lost):
    assert "unconfirmed" in str(lost)


def test_a_lost_answer_never_says_nothing_changed(lost):
    assert "Nothing was claimed" not in str(lost)


def test_a_lost_answer_makes_no_local_claim(lost, monkeypatch):
    monkeypatch.setattr(hubclient, "configured", lambda: None)
    assert taskops.listing() == []


# --- local requests, without a hub ---------------------------------------------------------------


@pytest.fixture
def local(monkeypatch, tmp_path):
    """Two live sessions on this machine, a lead named coordinator, and recorded deliveries."""
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path))
    monkeypatch.setattr(hubclient, "configured", lambda: None)
    sessions = {"claude": [LiveSession("claude", "lead", delivery="inbox")],
                "codex": [LiveSession("codex", "peer", delivery="inbox")]}
    monkeypatch.setattr(router, "list_all", lambda harness=None: sessions.get(harness, []) if harness
                        else sessions["claude"] + sessions["codex"])
    pushed, filed = [], []

    class Adapter:
        @staticmethod
        def deliver(session, wrapped):
            pushed.append((session.address, wrapped))
            return "delivered"
    monkeypatch.setattr(router, "ADAPTERS", {"claude": Adapter, "codex": Adapter})
    monkeypatch.setattr(router, "_append_fyi", lambda target, sender, text: filed.append((target, text)))
    team.set_name("coordinator", "claude:lead", lambda address: True)
    team.join("release", "claude:lead")
    team.join("release", "codex:peer")
    return pushed, filed


@pytest.fixture
def local_request(local):
    return router.send("#release", "Ship it", None, "user", message_id="b" * 32)


def test_a_local_human_request_is_owned_by_the_lead(local_request):
    assert local_request["task"]["owner"] == "claude:lead"


def test_a_local_human_request_wakes_only_the_lead(local, local_request):
    pushed, _ = local
    assert [address for address, _ in pushed] == ["claude:lead"]


def test_a_local_human_request_reaches_the_others_as_context(local, local_request):
    _, filed = local
    assert [target for target, _ in filed] == ["codex:peer"]


def test_a_retried_local_request_finds_the_same_task(local, local_request):
    again = router.send("#release", "Ship it", None, "user", message_id="b" * 32)
    assert again["task"]["key"] == local_request["task"]["key"]


def test_a_local_request_without_a_live_lead_is_unassigned(local):
    taskops.local_call(lambda db: tasks.set_lead(db, "release", "local/claude:gone", "user"))
    assert router.send("#release", "Ship it", None, "user", message_id="c" * 32)["task"]["owner"] is None


def test_a_local_observer_cannot_answer_by_leaving_the_task_out(local, local_request):
    peer = LiveSession("codex", "peer")
    with pytest.raises(router.LiveError):
        router.send("#release", "Done!", peer, message_id="d" * 32)


def test_a_local_observer_may_post_about_something_else(local, local_request):
    peer = LiveSession("codex", "peer")
    receipt = router.send("#release", "lunch?", peer, message_id="d" * 32, about="other")
    assert receipt["topic"] == "release"


def test_the_local_owner_reports_progress(local, local_request):
    task = taskops.update(local_request["task"]["key"], "claude:lead", local_request["task"]["version"], "working")
    assert task["state"] == "working"


def test_a_local_grant_wakes_the_collaborator(local, local_request):
    pushed, _ = local
    taskops.delegate(local_request["task"]["key"], "claude:lead", local_request["task"]["version"], "codex:peer",
                     "review", "check the diff")
    assert pushed[-1][0] == "codex:peer"


@pytest.fixture
def bridged(local):
    """What the bridge does with a hub delivery for an observer and for the owner."""
    from one_conv.live import bridge
    base = {"from": "workspace/user", "reply": "expected", "topic": "general", "text": "Book the train",
            "id": "1" * 32, "task": TASK}
    bridge.deliver_inbound({**base, "to": "codex:peer", "mode": "fyi", "role": "observer"})
    bridge.deliver_inbound({**base, "to": "claude:lead", "mode": "push", "role": "owner"})
    return local


def test_the_bridge_files_an_observer_copy_as_context(bridged):
    _, filed = bridged
    assert "Context only" in filed[-1][1]


def test_the_bridge_wakes_the_owner_with_its_instructions(bridged):
    pushed, _ = bridged
    assert "You own this request" in pushed[-1][1]


# --- the CLI ------------------------------------------------------------------------------------


@pytest.fixture
def cli(local):
    return CliRunner(), build_live_group(["one-conv"])


@pytest.mark.parametrize("command", [["task", "--help"], ["task", "list", "--help"], ["task", "show", "--help"],
                                     ["task", "claim", "--help"], ["task", "update", "--help"],
                                     ["task", "delegate", "--help"], ["lead", "--help"], ["lead", "set", "--help"]])
def test_every_task_command_has_help_with_examples(cli, command):
    runner, group = cli
    assert "Examples:" in runner.invoke(group, command).output


def test_the_cli_lists_a_local_task_as_json(cli, local_request):
    runner, group = cli
    listed = json.loads(runner.invoke(group, ["task", "list", "--json"]).output)
    assert listed[0]["key"] == local_request["task"]["key"]


def test_the_cli_refuses_an_update_from_a_non_owner(cli, local_request):
    runner, group = cli
    result = runner.invoke(group, ["task", "update", local_request["task"]["key"], "--version",
                                   str(local_request["task"]["version"]), "--state", "done", "--as", "codex:peer"])
    assert result.exit_code != 0


def test_the_cli_sets_a_local_lead(cli):
    runner, group = cli
    assert "#release: @peer" in runner.invoke(group, ["lead", "set", "#release", "@peer"]).output


def test_send_requires_task_and_version_together(cli):
    runner, group = cli
    assert runner.invoke(group, ["send", "#release", "hi", "--task", KEY, "--yes"]).exit_code == 2
