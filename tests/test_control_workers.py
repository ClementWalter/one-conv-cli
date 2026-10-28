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
"""The Boat client never repeats an unconfirmed effect, and box profiles load what the spawner box needs."""

import json
import os
import subprocess

import pytest

from one_conv.control.workers import (
    DEFAULT_PROFILE,
    BoatCLI,
    CloudBox,
    Unconfirmed,
    WorkerError,
    machine_status,
    private_json,
)




@pytest.mark.parametrize("state", ["ready", "idle", "running"])
def test_all_documented_running_states_are_available(state):
    assert machine_status(state) == "available"


@pytest.fixture
def box_config():
    return {"boat": object(), "public_hub_url": "https://hub.example.test"}


def test_a_box_profile_defaults_to_the_default_profile(tmp_path, box_config):
    assert CloudBox(tmp_path, box_config).profiles["default"] == DEFAULT_PROFILE


# What the deployed controller's config still holds from per-job cloud workers.
RETIRED_PROFILE = {"repositories": ["ClementWalter/claudine", "ClementWalter/one-conv-cli"],
                   "work_repository": "ClementWalter/one-conv-cli", "skills": ["brain"], "slots": 4,
                   "environment": "oneconv-workers", "boat_agent_credentials": True, "probe_enabled": True,
                   "revisions": {"ClementWalter/one-conv-cli": "d" * 40}}


def test_a_profile_with_retired_worker_fields_loads(tmp_path, box_config):
    box = CloudBox(tmp_path, {**box_config, "profiles": {"default": RETIRED_PROFILE}})
    assert box.profiles["default"]["environment"] == "oneconv-workers"


def test_a_profile_drops_its_pinned_revisions(tmp_path, box_config):
    box = CloudBox(tmp_path, {**box_config, "profiles": {"default": RETIRED_PROFILE}})
    assert "revisions" not in box.profiles["default"]


@pytest.mark.parametrize("profile", [{"unknown": 1}, {"type": "huge"}, {"repositories": ["ClementWalter/other"]},
                                     {"boat_agent_credentials": "yes"}, {"environment": "Bad Name"}])
def test_an_invalid_box_profile_is_refused(tmp_path, box_config, profile):
    with pytest.raises(ValueError):
        CloudBox(tmp_path, {**box_config, "profiles": {"default": profile}})


def test_a_former_worker_pools_token_is_found_by_its_node(tmp_path, box_config):
    (tmp_path / "workers.json").write_text(json.dumps({"jobs": {}, "pools": {"p1": {"id": "p1", "node": "cloudbox"}}}))
    assert CloudBox(tmp_path, box_config).pool_token_path("cloudbox") == tmp_path / "tokens" / "p1.json"


def test_a_node_without_a_former_pool_has_no_pool_token(tmp_path, box_config):
    assert CloudBox(tmp_path, box_config).pool_token_path("cloudbox") is None


def test_an_unreadable_worker_state_has_no_pool_token(tmp_path, box_config):
    (tmp_path / "workers.json").write_text("not json")
    assert CloudBox(tmp_path, box_config).pool_token_path("cloudbox") is None


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


def test_boat_reports_a_deleted_sandbox_as_gone(monkeypatch):
    from one_conv.control.workers import Gone
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 1, '{"code":"not_found","error":"not found (404)","event":"error","status":404}', ""))
    with pytest.raises(Gone):
        BoatCLI().info("bx_deleted")


def test_boat_reports_another_failure_as_unconfirmed(monkeypatch):
    from one_conv.control.workers import Gone
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "timeout"))
    with pytest.raises(Unconfirmed) as raised:
        BoatCLI().info("bx_known")
    assert not isinstance(raised.value, Gone)
