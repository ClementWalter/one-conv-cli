"""Durable worker jobs reserve boxes and native agents across retries and crashes."""

import json
import os
import subprocess
import sys
import threading

import pytest

from one_conv.control.workers import (
    DEFAULT_PROFILE,
    BoatCLI,
    PreflightRefused,
    Unconfirmed,
    WorkerError,
    WorkerManager,
    machine_status,
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
        self.state = "running"

    def credentials(self, profile):
        return {"service_account": "private-service-account"}

    def create(self, profile, pool_id):
        self.creates += 1
        if self.lose_create:
            raise Unconfirmed("a private provider diagnostic")
        return f"bx_{self.creates}"

    def info(self, box):
        return {"id": box, "state": self.state}

    def bootstrap(self, box, payload):
        self.starts.append((box, payload))
        if self.lose_bootstrap:
            raise Unconfirmed("a private bootstrap diagnostic")
        return {"status": "stopped"} if payload["operation"] == "stop" else self.health

    def stop_pool(self, box):
        self.pools_stopped.append(box)
        self.state = "stopped"

    def resume_pool(self, box):
        self.pools_resumed.append(box)
        self.state = "running"


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


@pytest.mark.parametrize("args", [["--api-key", "synthetic"], ["--api-key=synthetic"]])
def test_public_profiles_cannot_contain_native_secret_arguments(tmp_path, config, args):
    harnesses = {**DEFAULT_PROFILE["harness_args"], "omp": args}
    with pytest.raises(ValueError, match="private environment references"):
        WorkerManager(tmp_path / "workers", {**config, "profiles": {"default": {"harness_args": harnesses}}})


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


def test_automatic_pool_stop_reuses_the_known_box(provisioned, boat):
    boat.state = "stopped"
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert boat.creates == 1


def test_automatic_pool_stop_resumes_the_known_box(provisioned, boat):
    boat.state = "stopped"
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert boat.pools_resumed == ["bx_1"]


@pytest.mark.parametrize("state", ["ready", "idle", "running"])
def test_all_documented_running_states_reuse_the_tracked_pool(provisioned, spec, boat, state):
    boat.state = state
    provisioned.submit({**spec, "name": "builder"}, "request-2")
    provisioned.run_once()
    assert boat.creates == 1


def test_api_archived_state_resumes_the_tracked_pool(provisioned, boat):
    boat.state = "archived"
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert boat.pools_resumed == ["bx_1"]


@pytest.mark.parametrize("state", ["archiving", "provisioning", "error", "up", "unknown"])
def test_transient_or_unknown_states_are_not_running_capacity(state):
    assert machine_status(state) is None


@pytest.mark.parametrize("operation,states", [("stop", ["archiving", "archived"]),
                                            ("stop", ["stopping", "stopped"]),
                                            ("resume", ["provisioning", "ready"]),
                                            ("resume", ["provisioned", "idle"]),
                                            ("resume", ["cloning", "running"])])
def test_known_box_transitions_poll_for_documented_terminal_states(monkeypatch, operation, states):
    boat, commands = BoatCLI(), []
    responses = iter(states)

    def call(args, **kwargs):
        commands.append(args)
        return {"id": "bx_tracked", "state": next(responses)} if args[0] == "info" else {}

    monkeypatch.setattr(boat, "_call", call)
    monkeypatch.setattr("one_conv.control.workers.time.sleep", lambda duration: None)
    (boat.stop_pool if operation == "stop" else boat.resume_pool)("bx_tracked")
    assert commands == [[operation, "bx_tracked"], ["info", "bx_tracked"], ["info", "bx_tracked"]]


def test_a_missing_stop_ack_is_confirmed_without_repeating_the_action(monkeypatch):
    boat, commands = BoatCLI(), []

    def call(args, **kwargs):
        commands.append(args)
        if args[0] == "stop":
            raise Unconfirmed("lost acknowledgement")
        return {"id": "bx_tracked", "state": "archived"}

    monkeypatch.setattr(boat, "_call", call)
    boat.stop_pool("bx_tracked")
    assert commands == [["stop", "bx_tracked"], ["info", "bx_tracked"]]


def test_stop_timeout_does_not_claim_a_snapshot_is_complete(monkeypatch):
    boat = BoatCLI()
    ticks = iter([0, 1, 2, 181, 182])
    monkeypatch.setattr(boat, "_call", lambda args, **kwargs: {"id": "bx_tracked", "state": "archiving"})
    monkeypatch.setattr("one_conv.control.workers.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("one_conv.control.workers.time.sleep", lambda duration: None)
    with pytest.raises(Unconfirmed):
        boat.stop_pool("bx_tracked")


def test_explicit_resume_inspects_an_automatically_stopped_pool(provisioned, boat):
    boat.state = "stopped"
    provisioned.resume_pool(provisioned.list()["pools"][0]["id"])
    assert boat.pools_resumed == ["bx_1"]


def test_explicit_stop_confirms_an_already_archived_pool(provisioned, boat):
    boat.state = "archived"
    provisioned.stop_pool(provisioned.list()["pools"][0]["id"])
    assert provisioned.status("request-1")["status"] == "stopped"


@pytest.fixture
def resumed_after_ambiguous_stop(provisioned, boat):
    provisioned._update("request-1", status="ready", address="worker-node/claude:existing")

    def stop(box):
        raise Unconfirmed("stop outcome is unconfirmed")

    boat.stop_pool = stop
    pool = provisioned.list()["pools"][0]["id"]
    provisioned.stop_pool(pool)
    provisioned.resume_pool(pool)
    return provisioned, boat


def test_recheck_can_recover_after_an_ambiguous_stop(resumed_after_ambiguous_stop):
    manager, _ = resumed_after_ambiguous_stop
    assert manager.recheck("request-1")["status"] == "queued"


def test_ambiguous_stop_recovery_never_creates_a_replacement(resumed_after_ambiguous_stop):
    manager, boat = resumed_after_ambiguous_stop
    manager.recheck("request-1")
    manager.run_once()
    assert boat.creates == 1


def test_ambiguous_stop_recovery_preserves_the_native_claim(resumed_after_ambiguous_stop):
    manager, boat = resumed_after_ambiguous_stop
    manager.recheck("request-1")
    manager.run_once()
    assert (boat.starts[-1][0], boat.starts[-1][1]["job_id"], boat.starts[-1][1]["restart"]) == ("bx_1", "request-1", False)


@pytest.fixture
def crashed_pool_stop(provisioned, boat, tmp_path):
    provisioned._update("request-1", status="ready", address="worker-node/claude:existing", health={"registered": True})
    marker = tmp_path / "provider-stop-effect"
    script = """
import os
import sys
from pathlib import Path
from one_conv.control.workers import WorkerManager
class CrashBoat:
    def info(self, box):
        return {'id': box, 'state': 'idle'}
    def stop_pool(self, box):
        with Path(sys.argv[3]).open('w') as stream:
            stream.write(box)
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(99)
WorkerManager(Path(sys.argv[1]), {'boat': CrashBoat()}).stop_pool(sys.argv[2])
"""
    result = subprocess.run([sys.executable, "-O", "-c", script, str(provisioned.directory),
                             provisioned.list()["pools"][0]["id"], str(marker)], check=False,
                            capture_output=True, text=True, timeout=10)
    manager = WorkerManager(provisioned.directory, provisioned.config)
    manager.run_once()
    return manager, boat, result, marker


def test_pool_stop_crash_reaches_the_provider_effect(crashed_pool_stop):
    _, _, result, marker = crashed_pool_stop
    assert (result.returncode, marker.read_text()) == (99, "bx_1")


def test_pool_stop_crash_preserves_an_unconfirmed_job(crashed_pool_stop):
    manager, _, _, _ = crashed_pool_stop
    assert manager.status("request-1")["status"] == "unconfirmed"


def test_pool_stop_crash_clears_live_health_before_the_effect(crashed_pool_stop):
    manager, _, _, _ = crashed_pool_stop
    assert manager.status("request-1")["health"]["registered"] is False


def test_pool_stop_crash_does_not_allocate_a_replacement(crashed_pool_stop):
    _, boat, _, _ = crashed_pool_stop
    assert boat.creates == 1


def test_pool_stop_crash_keeps_the_native_name_reserved(crashed_pool_stop, spec):
    manager, _, _, _ = crashed_pool_stop
    with pytest.raises(ValueError, match="active job"):
        manager.submit(spec, "request-2")


def test_automatic_pool_stop_invalidates_other_old_jobs(provisioned, spec, boat):
    provisioned.submit({**spec, "name": "builder"}, "request-2")
    provisioned.run_once()
    boat.state = "stopped"
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert provisioned.status("request-2")["status"] == "stopped"


def test_ready_worker_recheck_does_not_create_a_second_box(provisioned, boat):
    provisioned._update("request-1", status="ready")
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert boat.creates == 1


def test_ready_worker_setup_failure_keeps_its_active_reservation(provisioned, boat):
    provisioned._update("request-1", status="ready", address="worker-node/claude:existing")
    boat.health = {"status": "failed", "stage": "github-vault"}
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert provisioned.status("request-1")["status"] == "unconfirmed"


def test_ready_worker_setup_failure_preserves_its_known_address(provisioned, boat):
    provisioned._update("request-1", status="ready", address="worker-node/claude:existing")
    boat.health = {"status": "failed", "stage": "github-vault"}
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert provisioned.status("request-1")["address"] == "worker-node/claude:existing"


def test_an_automatically_stopped_pool_cannot_restore_an_old_address(provisioned, boat):
    provisioned._update("request-1", status="ready", address="worker-node/claude:existing")
    boat.state = "stopped"
    boat.health = {"status": "failed", "stage": "github-vault"}
    provisioned.recheck("request-1")
    provisioned.run_once()
    assert provisioned.status("request-1")["address"] is None


def test_failed_preflight_does_not_free_an_existing_native_name(provisioned, spec, boat):
    provisioned._update("request-1", status="ready", address="worker-node/claude:existing")

    def refuse(profile):
        raise PreflightRefused("unsafe environment")

    boat.preflight = refuse
    provisioned.recheck("request-1")
    provisioned.run_once()
    with pytest.raises(ValueError, match="active job"):
        provisioned.submit(spec, "request-2")


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


def test_scp_uses_its_successful_transport_exit_status(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, "Copied private file to the sandbox.\n", ""))
    assert BoatCLI()._call(["scp", "payload.json", "bx_tracked:/tmp/payload.json"]) == {}


def test_scp_status_text_cannot_hide_a_failed_copy(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 1, "Copied private file to the sandbox.\n", "private transport diagnostic"))
    with pytest.raises(Unconfirmed):
        BoatCLI()._call(["scp", "payload.json", "bx_tracked:/tmp/payload.json"])


def test_non_json_scp_status_does_not_interrupt_the_verified_helper_flow(monkeypatch):
    def run(argv, **kwargs):
        if argv[1] == "scp":
            return subprocess.CompletedProcess(argv, 0, "Copied private file to the sandbox.\n", "")
        output = '{"status":"auth_needed","stage":"model-probe"}' if "helper.py" in argv[-1] else ""
        receipt = {"success": True, "exitCode": 0, "stdout": output, "stdoutTruncated": False,
                   "timedOut": False, "launcherFailed": False, "oomKilled": False}
        return subprocess.CompletedProcess(argv, 0, json.dumps(receipt), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert BoatCLI().bootstrap("bx_tracked", {"private": "payload"}) == {"status": "auth_needed", "stage": "model-probe"}


@pytest.fixture
def boat_creation_events():
    return [{"event": "created", "id": "bx_fixture", "ttlSeconds": 3600},
            {"event": "state", "id": "bx_fixture", "state": "provisioning"},
            {"event": "state", "id": "bx_fixture", "state": "ready"},
            {"event": "ready", "id": "bx_fixture", "state": "ready"}]


def test_boat_create_accepts_a_complete_jsonl_receipt(monkeypatch, boat_creation_events):
    output = "\n".join(json.dumps(row) for row in boat_creation_events)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""))
    assert BoatCLI().create(DEFAULT_PROFILE, "pool-id") == "bx_fixture"


@pytest.mark.parametrize("output", ['{"id":"bx_fixture"}', '{"sandbox":{"id":"bx_fixture"}}',
                                   '{\n"event":"ready",\n"id":"bx_fixture",\n"state":"ready"\n}'])
def test_boat_create_preserves_complete_single_json_receipts(monkeypatch, output):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""))
    assert BoatCLI().create(DEFAULT_PROFILE, "pool-id") == "bx_fixture"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_boat_creation_requires_the_terminal_ready_event(boat_creation_events, count):
    output = "\n".join(json.dumps(row) for row in boat_creation_events[:count])
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "new")


@pytest.mark.parametrize("index", [0, 1, 2])
def test_boat_creation_requires_consistent_ids(boat_creation_events, index):
    boat_creation_events[index]["id"] = "bx_different"
    output = "\n".join(json.dumps(row) for row in boat_creation_events)
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "new")


@pytest.mark.parametrize("suffix", ['\n{"event":', '\n{"event":"error","id":"bx_fixture"}', '\nprivate non-json'])
def test_boat_creation_rejects_malformed_or_failed_trailers(boat_creation_events, suffix):
    output = "\n".join(json.dumps(row) for row in boat_creation_events) + suffix
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "new")


@pytest.mark.parametrize("field,value", [("event", "state"), ("state", "provisioning"), ("id", "../invalid")])
def test_boat_creation_rejects_invalid_terminal_receipts(boat_creation_events, field, value):
    boat_creation_events[-1][field] = value
    output = "\n".join(json.dumps(row) for row in boat_creation_events)
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "new")


@pytest.mark.parametrize("output", ['{"id":"bx_fixture","padding":"' + "x" * 2_000_000 + '"}',
                                   "\n".join('{"event":"state","id":"bx_fixture"}' for _ in range(129))])
def test_boat_receipts_are_bounded_before_parsing(output):
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "new")


def test_non_creation_operations_do_not_accept_progress_streams(boat_creation_events):
    output = "\n".join(json.dumps(row) for row in boat_creation_events)
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "exec")


@pytest.fixture
def boat_resume_events():
    return [{"event": "accepted", "id": "bx_fixture"},
            {"event": "state", "id": "bx_fixture", "state": "ready"},
            {"event": "state", "id": "bx_fixture", "state": "cloning"},
            {"event": "state", "id": "bx_fixture", "state": "idle"},
            {"event": "ready", "id": "bx_fixture", "state": "idle"}]


def test_actual_resume_jsonl_waits_for_terminal_ready_idle(boat_resume_events):
    output = "\n".join(json.dumps(row) for row in boat_resume_events)
    assert BoatCLI._receipt(output, "resume") == {"event": "ready", "id": "bx_fixture", "state": "idle"}


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_resume_state_ready_does_not_replace_a_terminal_ready_event(boat_resume_events, count):
    output = "\n".join(json.dumps(row) for row in boat_resume_events[:count])
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "resume")


def test_resume_stream_cannot_change_box_identity(boat_resume_events):
    boat_resume_events[2]["id"] = "bx_other"
    output = "\n".join(json.dumps(row) for row in boat_resume_events)
    with pytest.raises(Unconfirmed):
        BoatCLI._receipt(output, "resume")


@pytest.fixture
def agent_environment(monkeypatch):
    boat = BoatCLI()
    raw = {"environment": {"safeForThirdParties": False, "proxyAiCredentials": False,
                           "effective": {"passGithub": False, "passSecrets": False,
                                         "passSandboxCredentials": False, "passAgentsCredentials": True}}}
    monkeypatch.setattr(boat, "_call", lambda *args, **kwargs: raw)
    return boat, raw


def test_dedicated_native_credentials_environment_is_verified(agent_environment):
    boat, _ = agent_environment
    profile = {**DEFAULT_PROFILE, "boat_agent_credentials": True, "environment": "oneconv-workers"}
    assert boat.preflight(profile)["agent_credentials"] is True


@pytest.mark.parametrize("field,value", [("passGithub", True), ("passSecrets", True),
                                        ("passSandboxCredentials", True), ("passAgentsCredentials", False)])
def test_unsafe_effective_environment_toggles_are_refused(agent_environment, field, value):
    boat, raw = agent_environment
    raw["environment"]["effective"][field] = value
    profile = {**DEFAULT_PROFILE, "boat_agent_credentials": True, "environment": "oneconv-workers"}
    with pytest.raises(WorkerError, match="pass only native agent credentials"):
        boat.preflight(profile)


def test_a_broad_personal_environment_cannot_pass_agent_credentials(agent_environment):
    boat, _ = agent_environment
    with pytest.raises(WorkerError, match="dedicated worker environment"):
        boat.preflight({**DEFAULT_PROFILE, "boat_agent_credentials": True, "environment": "claudine"})


@pytest.mark.parametrize("enabled,expected", [(False, True), (True, False)])
def test_native_credentials_profiles_preserve_boat_agent_auth(monkeypatch, enabled, expected):
    boat, commands = BoatCLI(), []

    def call(args, **kwargs):
        commands.append(args)
        return {"id": "bx_created"}

    monkeypatch.setattr(boat, "_call", call)
    monkeypatch.setattr(boat, "preflight", lambda profile: {"agent_credentials": enabled})
    boat.create({**DEFAULT_PROFILE, "boat_agent_credentials": enabled}, "pool-id")
    assert ("--no-env" in commands[0]) is expected
