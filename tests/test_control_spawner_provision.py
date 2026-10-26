"""The controller builds the cloud spawner box from code: creates it, then provisions it on every new revision."""

import json
import time
from types import SimpleNamespace

import pytest

from one_conv.control import runtime
from one_conv.control.runtime import provision_spawner
from one_conv.control.workers import DEFAULT_PROFILE
from one_conv.live.hub import Hub

SPAWNER = {"node": "cloudbox", "git_user": {"name": "Ada", "email": "ada@example.test"}}


class StubBoat:
    """Records Boat calls against one sandbox; bootstrap answers with the given health."""

    def __init__(self, state="idle", health=None):
        self.state, self.health, self.calls, self.payloads = state, health or {"status": "ready"}, [], []

    def create(self, profile, pool_id, *, auto_stop=True):
        self.calls.append(("create", pool_id, auto_stop))
        return "bx_new"

    def info(self, box):
        return {"id": box, "state": self.state}

    def credentials(self, profile):
        return {"service_account": "op-token"}

    def bootstrap(self, box, payload):
        self.calls.append(("bootstrap", box))
        self.payloads.append(payload)
        return self.health


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_revision", lambda: "rev-1")
    (tmp_path / "workers.json").write_text(json.dumps({"jobs": {}, "pools": {}}))
    return SimpleNamespace(directory=tmp_path, state_path=tmp_path / "workers.json", boat=StubBoat(),
                           profiles={"default": DEFAULT_PROFILE},
                           config={"public_hub_url": "https://hub.example.test", "hub": Hub(str(tmp_path / "hub.db"))})


def test_a_missing_box_is_created(manager):
    provision_spawner(manager, dict(SPAWNER), wanted=False)
    assert manager.boat.calls[0] == ("create", "cloudbox", False)


def test_a_created_box_is_remembered(manager):
    provision_spawner(manager, dict(SPAWNER), wanted=False)
    assert json.loads((manager.directory / "spawner.json").read_text())["box"] == "bx_new"


def test_a_configured_box_is_provisioned(manager):
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    assert manager.boat.calls == [("bootstrap", "bx_cloud")]


def test_the_box_gets_a_node_token_the_hub_accepts(manager):
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    assert manager.config["hub"].node_for(manager.boat.payloads[0]["hub"]["token"]) == "cloudbox"


def test_the_box_reuses_the_token_of_a_worker_pool_on_its_node(manager):
    (manager.directory / "workers.json").write_text(json.dumps({"jobs": {}, "pools": {"p1": {"id": "p1", "node": "cloudbox"}}}))
    (manager.directory / "tokens").mkdir()
    (manager.directory / "tokens" / "p1.json").write_text(json.dumps({"token": "pool-token"}))
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    assert manager.boat.payloads[0]["hub"]["token"] == "pool-token"


def test_the_box_reads_credentials_from_boats_secrets_file(manager):
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    assert manager.boat.payloads[0]["spawner"]["env_file"] == runtime.BOAT_SECRETS_FILE


def test_a_provisioned_box_is_not_provisioned_again(manager):
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    assert provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False) == "current"


def test_a_new_revision_provisions_the_box_again(manager, monkeypatch):
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    monkeypatch.setattr(runtime, "_revision", lambda: "rev-2")
    assert provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False) == "provisioned"


def test_changed_settings_provision_the_box_again(manager):
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    assert provision_spawner(manager, {**SPAWNER, "box": "bx_cloud", "priority": 5}, wanted=False) == "provisioned"


def test_a_box_a_request_waits_for_is_not_provisioned(manager):
    assert provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=True) == "waiting"


def test_a_sleeping_box_is_provisioned_when_it_runs(manager):
    manager.boat.state = "stopped"
    assert provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False) == "asleep"


def test_a_failed_provisioning_is_retried(manager):
    manager.boat.health = {"status": "failed", "stage": "github-login"}
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    record = json.loads((manager.directory / "spawner.json").read_text())
    record["tried"] = time.time() - runtime.SPAWNER_RETRY_SECONDS - 1
    (manager.directory / "spawner.json").write_text(json.dumps(record))
    manager.boat.health = {"status": "ready"}
    assert provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False) == "provisioned"


def test_a_failed_provisioning_waits_before_retrying(manager):
    manager.boat.health = {"status": "failed", "stage": "github-login"}
    provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False)
    assert provision_spawner(manager, {**SPAWNER, "box": "bx_cloud"}, wanted=False) == "waiting"


def test_a_deleted_box_is_replaced(manager, monkeypatch):
    from one_conv.control.workers import Gone

    def gone(box):
        raise Gone("deleted")
    monkeypatch.setattr(manager.boat, "info", gone)
    provision_spawner(manager, {**SPAWNER, "box": "bx_deleted"}, wanted=False)
    assert manager.boat.calls[0] == ("create", "cloudbox", False)


def test_the_box_the_controller_created_outranks_the_configured_one(manager):
    (manager.directory / "spawner.json").write_text(json.dumps({"box": "bx_created"}))
    provision_spawner(manager, {**SPAWNER, "box": "bx_configured"}, wanted=False)
    assert manager.boat.calls == [("bootstrap", "bx_created")]


def test_git_identity_is_left_to_the_box_when_not_configured(manager):
    provision_spawner(manager, {"node": "cloudbox", "box": "bx_cloud"}, wanted=False)
    assert manager.boat.payloads[0]["git_user"] is None


def test_a_spawner_block_needs_only_a_node():
    runtime.check_spawner({"node": "cloudbox"}, {"default": DEFAULT_PROFILE})


@pytest.mark.parametrize("spawner", [
    {"box": "bx_1"},
    {"node": "Bad Node"},
    {"node": "cloudbox", "colour": "red"},
    {"node": "cloudbox", "profile": "absent"},
    {"node": "cloudbox", "git_user": {"name": "Ada"}},
])
def test_a_bad_spawner_block_is_refused(spawner):
    with pytest.raises(ValueError, match="spawner block"):
        runtime.check_spawner(spawner, {"default": DEFAULT_PROFILE})


def test_a_created_box_is_provisioned_at_once(manager):
    assert provision_spawner(manager, dict(SPAWNER), wanted=False) == "provisioned"
