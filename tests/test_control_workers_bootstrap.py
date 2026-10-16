"""Cloud bootstrap validates private payloads and verifies native assistant answers."""

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from one_conv.control import worker_bootstrap as bootstrap
from one_conv.control.workers import DEFAULT_PROFILE


@pytest.fixture
def payload():
    return {"operation": "start", "job_id": "job-1", "pool_id": "pool-1", "service_account": "private-op-token",
            "spec": {"name": "reviewer", "harness": "claude", "model": None, "topic": "release",
                     "profile": "default", "prompt": "Review this repository."},
            "profile": copy.deepcopy(DEFAULT_PROFILE),
            "hub": {"url": "https://hub.example.test", "node": "worker-pool", "token": "private-hub-token"}}


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("field,value", [("job_id", "../escape"), ("service_account", "white space"),
                                        ("operation", "shell")])
def test_bad_payloads_are_refused_before_bootstrap(payload, field, value):
    with pytest.raises(bootstrap.BootstrapError, match="payload"):
        bootstrap.validate_payload({**payload, field: value})


@pytest.mark.parametrize("repository", ["org/..", "org/repo.git", "org/repo;touch", "../escape", "owner/repo\x00"])
def test_clone_names_cannot_inject_commands_or_paths(payload, repository):
    payload["profile"]["repositories"] = [repository]
    with pytest.raises(bootstrap.BootstrapError, match="payload"):
        bootstrap.validate_payload(payload)


def test_private_payload_is_deleted_before_it_is_used(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text('{"token":"private"}')
    bootstrap.read_private(path)
    assert not path.exists()


def test_even_invalid_private_payload_is_removed(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text("{")
    try:
        bootstrap.read_private(path)
    except ValueError:
        pass
    assert not path.exists()


@pytest.mark.parametrize("harness,output", [
    ("claude", '{"type":"result","is_error":false,"result":"ONE_CONV_READY"}'),
    ("codex", '{"type":"item.completed","item":{"type":"agent_message","text":"ONE_CONV_READY"}}'),
    ("omp", '{"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[{"type":"text","text":"ONE_CONV_READY"}]}}'),
])
def test_native_probe_requires_a_real_assistant_answer(harness, output):
    assert bootstrap.probe_answer(harness, output) is True


@pytest.mark.parametrize("harness,output", [
    ("claude", '{"type":"result","is_error":true,"result":"ONE_CONV_READY"}'),
    ("claude", '{"type":"user","result":"ONE_CONV_READY"}'),
    ("codex", '{"type":"item.completed","item":{"type":"tool_result","text":"ONE_CONV_READY"}}'),
    ("omp", '{"type":"message_end","message":{"role":"assistant","stopReason":"error","content":[{"type":"text","text":"ONE_CONV_READY"}]}}'),
    ("omp", "ONE_CONV_READY"),
])
def test_echoes_and_errors_are_not_model_readiness(harness, output):
    assert bootstrap.probe_answer(harness, output) is False


def test_disabling_a_probe_is_unverified(worker_home, monkeypatch, payload):
    payload["profile"]["probe_enabled"] = False
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    monkeypatch.setattr(bootstrap, "worktree", lambda data, env: worker_home)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: command)
    assert bootstrap.bootstrap(payload)["status"] == "unverified"


def test_a_rejected_model_is_auth_needed(worker_home, monkeypatch, payload):
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    monkeypatch.setattr(bootstrap, "worktree", lambda data, env: worker_home)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: command)
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "private error"))
    assert bootstrap.bootstrap(payload)["status"] == "auth_needed"


def test_missing_native_package_is_a_confirmed_failure(worker_home, monkeypatch, payload):
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    monkeypatch.setattr(bootstrap, "worktree", lambda data, env: worker_home)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: None)
    assert bootstrap.bootstrap(payload) == {"status": "failed", "stage": "harness-unavailable"}


def test_a_changed_request_cannot_reuse_remote_worker_state(worker_home, monkeypatch, payload):
    payload["profile"]["probe_enabled"] = False
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    monkeypatch.setattr(bootstrap, "worktree", lambda data, env: worker_home)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: command)
    bootstrap.bootstrap(payload)
    with pytest.raises(bootstrap.BootstrapError, match="request-conflict"):
        bootstrap.bootstrap({**payload, "spec": {**payload["spec"], "prompt": "other work"}})


def test_a_launch_marker_blocks_duplicate_native_launch(worker_home, payload):
    assert bootstrap._start(payload, {}, worker_home, worker_home / "job.json", {"phase": "launching"})["status"] == "unconfirmed"


def test_secrets_and_prompt_are_direct_native_arguments(worker_home, monkeypatch):
    bootstrap.write_private(bootstrap.root() / "credentials.json", {"GH_TOKEN": "private-gh-token"})
    path = worker_home / "run.json"
    prompt = "$(touch /tmp/do-not-create) ; `private`"
    bootstrap.write_private(path, {"harness": "claude", "model": "selected-model", "prompt": prompt})
    calls = []
    monkeypatch.setattr(os, "execvpe", lambda executable, args, env: calls.append((executable, args, env)))
    bootstrap.run_native(path)
    assert calls[0][1] == ["claude", "--model", "selected-model", prompt]


def test_native_secret_payload_disappears_before_exec(worker_home, monkeypatch):
    bootstrap.write_private(bootstrap.root() / "credentials.json", {"GH_TOKEN": "private-gh-token"})
    path = worker_home / "run.json"
    bootstrap.write_private(path, {"harness": "codex", "model": None, "prompt": "task"})
    monkeypatch.setattr(os, "execvpe", lambda *args: None)
    bootstrap.run_native(path)
    assert not path.exists()


def test_cloud_bootstrap_reads_only_requested_vault_fields(worker_home, monkeypatch, payload):
    calls = []
    monkeypatch.setattr(bootstrap, "install_op", lambda: Path("op"))

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["op", "read"]:
            output = "github-private-token"
        elif argv[-3:] == ["get-url", "origin"]:
            output = ""
        elif "get-url" in argv:
            output = f"https://github.com/ClementWalter/{Path(argv[2]).name}.git"
        else:
            output = "valid"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(bootstrap, "run", run)
    bootstrap.prepare_repositories(payload)
    assert [argv[2] for argv in calls if argv[:2] == ["op", "read"]] == [bootstrap.GITHUB_REF]


def test_provider_failures_do_not_expose_diagnostics(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "private-token", "private-token"))
    with pytest.raises(bootstrap.BootstrapError, match="^github-login$"):
        bootstrap.run(["gh", "auth", "login"], stage="github-login")


def test_bootstrap_state_is_private(worker_home):
    path = worker_home / "state.json"
    bootstrap.write_private(path, {"private": "data"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_all_native_probes_select_the_requested_model():
    assert bootstrap.probe_command("codex", "selected-model")[2:4] == ["--model", "selected-model"]


@pytest.fixture
def collector(worker_home, monkeypatch, payload):
    bootstrap.root().mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: None)

    class Watcher:
        """A resident collector process without provider requests."""

        pid = 1234

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Watcher())
    bootstrap._collector(payload, "claude:session-1", ["one-conv"])
    return json.loads((bootstrap.root() / "usage-map.json").read_text())


def test_usage_owner_mapping_uses_the_stable_human_name(collector):
    assert collector["owners"]["claude:session-1"]["cost_center_id"] == "reviewer"


def test_usage_owner_mapping_uses_a_qualified_native_worker_id(collector):
    assert collector["owners"]["claude:session-1"]["worker_id"] == "worker-pool/claude:session-1"


@pytest.mark.parametrize("forbidden", ["private-op-token", "private-hub-token", "Review this repository."])
def test_usage_owner_mapping_never_contains_prompt_or_credentials(collector, forbidden):
    assert forbidden not in json.dumps(collector)


def test_usage_owner_mapping_has_an_explicit_history_root(collector, worker_home):
    assert collector["histories"][0]["path"] == str(worker_home / ".claude/projects")


def test_usage_scope_is_honestly_unknown_without_an_account_label(collector):
    assert collector["histories"][0]["billing_scope"] == "unknown-pool-pool-1"
