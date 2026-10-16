"""Durable worker jobs reserve boxes and native agents across retries and crashes."""

import json
import os
import subprocess
import threading

import pytest

from one_conv.control.workers import (
    BoatCLI,
    Unconfirmed,
    WorkerError,
    WorkerManager,
    private_json,
)


@pytest.fixture
def spec():
    return {"name": "reviewer", "harness": "claude", "model": None, "topic": "release",
            "profile": "default", "prompt": "Review the selected repository."}


class StubBoat:
    """An in-memory provider with explicit ambiguous acknowledgements."""

    def __init__(self):
        self.creates = 0
        self.starts = []
        self.health = {"status": "auth_needed", "authenticated": False, "stage": "model-probe"}
        self.lose_create = False
        self.lose_bootstrap = False
        self.pools_stopped = []
        self.pools_resumed = []

    def credentials(self, profile):
        return {"service_account": "private-service-account"}

    def create(self, profile, pool_id):
        self.creates += 1
        if self.lose_create:
            raise Unconfirmed("a private provider diagnostic")
        return f"bx_{self.creates}"

    def info(self, box):
        return {"id": box}

    def bootstrap(self, box, payload):
        self.starts.append((box, payload))
        if self.lose_bootstrap:
            raise Unconfirmed("a private bootstrap diagnostic")
        return {"status": "stopped"} if payload["operation"] == "stop" else self.health

    def stop_pool(self, box):
        self.pools_stopped.append(box)

    def resume_pool(self, box):
        self.pools_resumed.append(box)


@pytest.fixture
def boat():
    return StubBoat()


@pytest.fixture
def config(boat):
    return {"boat": boat, "public_hub_url": "https://hub.example.test", "create_node_token": lambda node: "private-hub-token"}


@pytest.fixture
def manager(tmp_path, config):
    return WorkerManager(tmp_path / "workers", config)


@pytest.fixture
def provisioned(manager, spec):
    manager.submit(spec, "request-1")
    manager.run_once()
    return manager


def test_submit_returns_before_any_allocation(manager, spec, boat):
    manager.submit(spec, "request-1")
    assert boat.creates == 0


def test_submit_is_durable(manager, spec):
    manager.submit(spec, "request-1")
    assert json.loads(manager.state_path.read_text())["jobs"]["request-1"]["status"] == "queued"


def test_reusing_a_request_id_preserves_the_job(manager, spec):
    first = manager.submit(spec, "request-1")
    assert manager.submit(spec, "request-1") == first


def test_reusing_a_request_id_with_other_text_is_refused(manager, spec):
    manager.submit(spec, "request-1")
    with pytest.raises(ValueError, match="different spec"):
        manager.submit({**spec, "prompt": "Other task"}, "request-1")


def test_another_request_cannot_duplicate_an_active_name(manager, spec):
    manager.submit(spec, "request-1")
    with pytest.raises(ValueError, match="active job"):
        manager.submit(spec, "request-2")


@pytest.mark.parametrize("id", ["../escape", "", "a/b", "a" * 65])
def test_job_ids_cannot_escape_the_store(manager, spec, id):
    with pytest.raises(ValueError, match="job id"):
        manager.submit(spec, id)


@pytest.mark.parametrize("change", [
    {"name": "../escape"}, {"topic": "#Release"}, {"harness": "shell"}, {"profile": "personal"},
    {"model": "--arbitrary-flag"}, {"model": "bad\x00model"}, {"prompt": ""}, {"prompt": "a" * 128001},
    {"repositories": ["unrequested/private"]},
])
def test_untrusted_specs_fail_before_provisioning(manager, spec, change):
    with pytest.raises(ValueError):
        manager.submit({**spec, **change}, "request-1")


@pytest.mark.parametrize("profile", [{"slots": 11}, {"slots": True}, {"type": "xlarge"},
                                     {"repositories": ["org/../escape"]}, {"credential_refs": {"PATH": "op://secret"}}])
def test_invalid_profiles_cannot_allocate(tmp_path, config, profile):
    with pytest.raises(ValueError):
        WorkerManager(tmp_path / "workers", {**config, "profiles": {"default": profile}})


def test_native_authentication_failure_is_visible(provisioned):
    assert provisioned.status("request-1")["status"] == "auth_needed"


def test_missing_authentication_reuses_the_same_pool_on_retry(provisioned, boat):
    provisioned.retry("request-1")
    provisioned.run_once()
    assert boat.creates == 1


def test_multiple_agents_pack_into_one_pool(provisioned, spec, boat):
    provisioned.submit({**spec, "name": "builder"}, "request-2")
    provisioned.run_once()
    assert boat.creates == 1


def test_a_full_pool_allocates_another_box(tmp_path, config, spec, boat):
    manager = WorkerManager(tmp_path / "workers", {**config, "profiles": {"default": {"slots": 1}}})
    manager.submit(spec, "request-1")
    manager.run_once()
    manager.submit({**spec, "name": "builder"}, "request-2")
    manager.run_once()
    assert boat.creates == 2


def test_lost_create_acknowledgement_becomes_unconfirmed(manager, spec, boat):
    boat.lose_create = True
    manager.submit(spec, "request-1")
    assert manager.run_once()["status"] == "unconfirmed"


def test_lost_create_acknowledgement_is_never_retried(manager, spec, boat):
    boat.lose_create = True
    manager.submit(spec, "request-1")
    manager.run_once()
    manager.run_once()
    assert boat.creates == 1


def test_lost_create_requires_explicit_reconciliation(manager, spec, boat):
    boat.lose_create = True
    manager.submit(spec, "request-1")
    manager.run_once()
    with pytest.raises(ValueError, match="known safe setup"):
        manager.retry("request-1")


def test_reconciliation_reuses_the_selected_box(manager, spec, boat):
    boat.lose_create = True
    manager.submit(spec, "request-1")
    manager.run_once()
    manager.reconcile("request-1", "bx_inspected")
    manager.run_once()
    assert manager.status("request-1")["box_id"] == "bx_inspected"


def test_bootstrap_recheck_never_creates_a_replacement(manager, spec, boat):
    boat.lose_bootstrap = True
    manager.submit(spec, "request-1")
    manager.run_once()
    boat.lose_bootstrap = False
    manager.recheck("request-1")
    manager.run_once()
    assert boat.creates == 1


def test_a_released_controller_claim_is_not_replayed(manager, spec, boat):
    manager.submit(spec, "request-1")
    manager._update("request-1", status="provisioning", phase="creating")
    manager.run_once()
    assert boat.creates == 0


def test_a_released_controller_claim_is_honestly_unconfirmed(manager, spec):
    manager.submit(spec, "request-1")
    manager._update("request-1", status="provisioning", phase="creating")
    manager.run_once()
    assert manager.status("request-1")["status"] == "unconfirmed"


def test_state_is_private(provisioned):
    assert provisioned.state_path.stat().st_mode & 0o777 == 0o600


def test_tokens_are_excluded_from_public_status(provisioned):
    assert "private-hub-token" not in json.dumps(provisioned.list())


def test_provider_diagnostics_are_excluded_from_public_status(manager, spec, boat):
    boat.lose_create = True
    manager.submit(spec, "request-1")
    manager.run_once()
    assert "private provider diagnostic" not in json.dumps(manager.list())


def test_untrusted_health_values_cannot_exfiltrate_into_status(manager, spec, boat):
    boat.health = {"status": "auth_needed", "stage": "a secret credential with spaces", "authenticated": "private-provider-token"}
    manager.submit(spec, "request-1")
    manager.run_once()
    assert "secret" not in json.dumps(manager.status("request-1")["health"])


def test_unverified_capacity_is_explicit(provisioned):
    assert provisioned.list()["pools"][0]["active_capacity"] == "unverified"


def test_stopping_a_queued_worker_allocates_nothing(manager, spec, boat):
    manager.submit(spec, "request-1")
    manager.stop("request-1")
    assert boat.creates == 0


def test_stopping_one_worker_keeps_the_pool(provisioned):
    provisioned.stop("request-1")
    assert provisioned.list()["pools"][0]["status"] == "available"


def test_stopping_a_pool_uses_its_tracked_box(provisioned, boat):
    provisioned.stop_pool(provisioned.list()["pools"][0]["id"])
    assert boat.pools_stopped == ["bx_1"]


def test_stopping_a_pool_marks_its_agents_stopped(provisioned):
    provisioned.stop_pool(provisioned.list()["pools"][0]["id"])
    assert provisioned.status("request-1")["status"] == "stopped"


def test_resuming_a_pool_does_not_declare_old_agents_ready(provisioned):
    pool = provisioned.list()["pools"][0]["id"]
    provisioned.stop_pool(pool)
    provisioned.resume_pool(pool)
    assert provisioned.status("request-1")["status"] == "stopped"


def test_rechecking_after_resume_reuses_the_tracked_box(provisioned, boat):
    pool = provisioned.list()["pools"][0]["id"]
    provisioned.stop_pool(pool)
    provisioned.resume_pool(pool)
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert boat.creates == 1


def test_ten_resident_slots_can_be_configured(tmp_path, config):
    manager = WorkerManager(tmp_path, {**config, "profiles": {"default": {"slots": 10}}})
    assert manager.list()["profiles"]["default"]["slots"] == 10


def test_corrupt_state_fails_closed(manager, spec):
    manager.state_path.write_text("{")
    with pytest.raises(WorkerError, match="unreadable"):
        manager.submit(spec, "request-1")


def test_status_remains_available_during_a_slow_create(manager, spec, boat):
    entered, release = threading.Event(), threading.Event()

    def create(profile, pool):
        entered.set()
        release.wait(5)
        return "bx_1"

    boat.create = create
    manager.submit(spec, "request-1")
    thread = threading.Thread(target=manager.run_once)
    thread.start()
    try:
        entered.wait(5)
        assert manager.status("request-1")["phase"] == "creating"
    finally:
        release.set()
        thread.join(5)


def test_private_replacements_preserve_mode(tmp_path):
    path = tmp_path / "private.json"
    private_json(path, {"private": "value"})
    assert os.stat(path).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("contents", ["private-service-account\n", '{"service_account":"private-service-account"}'])
def test_cloud_service_account_file_avoids_environment_reads(tmp_path, monkeypatch, contents):
    path = tmp_path / "service-account"
    path.write_text(contents)
    path.chmod(0o600)
    boat = BoatCLI(service_account_file=path)
    monkeypatch.setattr(boat, "_call", lambda *args, **kwargs: pytest.fail("environment read is not authorized"))
    assert boat.credentials({}) == {"service_account": "private-service-account"}


@pytest.mark.parametrize("contents", ["two tokens", '{"service_account":"private","other":"field"}', "", '{"service_account":null}'])
def test_invalid_cloud_service_account_files_are_refused(tmp_path, contents):
    path = tmp_path / "service-account"
    path.write_text(contents)
    path.chmod(0o600)
    boat = BoatCLI(service_account_file=path)
    with pytest.raises(WorkerError, match="private valid token"):
        boat.credentials({})


def test_world_readable_service_account_files_are_refused(tmp_path):
    path = tmp_path / "service-account"
    path.write_text("private-token")
    path.chmod(0o644)
    with pytest.raises(WorkerError, match="private valid token"):
        BoatCLI(service_account_file=path).credentials({})


def test_service_account_file_symlinks_are_refused(tmp_path):
    path = tmp_path / "service-account"
    target = tmp_path / "actual-secret"
    target.write_text("private-token")
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(WorkerError, match="private valid token"):
        BoatCLI(service_account_file=path).credentials({})


def test_boat_exec_never_interpolates_the_payload_text(tmp_path, monkeypatch):
    commands = []
    payload = {"prompt": "$(touch /tmp/untrusted) ; `credential`", "token": "private-token"}
    boat = BoatCLI()

    def call(args, **kwargs):
        commands.append(args)
        if args[0] == "exec":
            return {"success": True, "exitCode": 0, "stdout": '{"status":"auth_needed"}',
                    "stdoutTruncated": False, "timedOut": False, "launcherFailed": False, "oomKilled": False}
        return {}

    monkeypatch.setattr(boat, "_call", call)
    boat.bootstrap("bx_known", payload)
    assert all(payload["prompt"] not in json.dumps(command) and payload["token"] not in json.dumps(command) for command in commands)


def test_boat_exec_flags_precede_the_remote_command_delimiter(monkeypatch):
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        response = {"success": True, "exitCode": 0, "stdout": "{}", "stdoutTruncated": False,
                    "timedOut": False, "launcherFailed": False, "oomKilled": False}
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    monkeypatch.setattr(subprocess, "run", run)
    BoatCLI()._exec("bx_known", ["python3", "/tmp/fixed-helper.py", "/tmp/private-payload.json"])
    assert commands == [["box", "exec", "bx_known", "--timeout", "600", "--json", "--no-update", "--",
                         "python3 /tmp/fixed-helper.py /tmp/private-payload.json"]]
