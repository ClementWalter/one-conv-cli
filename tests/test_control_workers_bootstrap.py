"""Cloud bootstrap validates private payloads and verifies native assistant answers."""

import copy
import hashlib
import io
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
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    return tmp_path


@pytest.fixture
def omp_release(worker_home, monkeypatch):
    binary = b"\x7fELF\x02\x01\x01" + bytes(11) + (62).to_bytes(2, "little") + bytes(44) + b"pinned release"

    class ReleaseResponse(io.BytesIO):
        """A pinned HTTPS response keeps transport tests independent of release size."""

        def geturl(self):
            return "https://release-assets.githubusercontent.com/pinned"

    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap, "OMP_RELEASES", {"x86_64": ("x64", len(binary), hashlib.sha256(binary).hexdigest(), 62)})
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: ReleaseResponse(binary))

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "omp/18.0.6\n" if argv[-1] == "--version"
                                           else "--no-tools --mode --approval-mode", "")

    monkeypatch.setattr(bootstrap, "run", run)
    return binary, ReleaseResponse


def test_official_omp_download_is_atomically_installed(omp_release, worker_home):
    binary, _ = omp_release
    assert bootstrap.install_omp().read_bytes() == binary


def test_installed_omp_is_executable(omp_release):
    assert bootstrap.install_omp().stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("delta", [-1, 1])
def test_omp_download_requires_the_exact_pinned_size(omp_release, monkeypatch, delta):
    binary, response = omp_release
    changed = binary[:-1] if delta < 0 else binary + b"x"
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: response(changed))
    with pytest.raises(bootstrap.BootstrapError, match="^omp-(checksum|download)$"):
        bootstrap.install_omp()


def test_omp_rejects_an_incorrect_release_checksum(omp_release, monkeypatch):
    binary, response = omp_release
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: response(binary[:-1] + b"x"))
    with pytest.raises(bootstrap.BootstrapError, match="^omp-checksum$"):
        bootstrap.install_omp()


def test_omp_rejects_an_insecure_download_redirect(omp_release, monkeypatch):
    binary, response = omp_release
    monkeypatch.setattr(response, "geturl", lambda self: "http://release.example.test/binary")
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: response(binary))
    with pytest.raises(bootstrap.BootstrapError, match="^omp-download$"):
        bootstrap.install_omp()


def test_omp_arm64_release_requires_the_arm64_machine(omp_release, monkeypatch):
    binary, response = omp_release
    changed = binary[:18] + (183).to_bytes(2, "little") + binary[20:]
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(bootstrap, "OMP_RELEASES", {"aarch64": ("arm64", len(changed), hashlib.sha256(changed).hexdigest(), 183)})
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: response(changed))
    assert bootstrap.install_omp().read_bytes() == changed


def test_unsupported_omp_platform_never_downloads(omp_release, monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("unsupported platform"))
    with pytest.raises(bootstrap.BootstrapError, match="^omp-architecture$"):
        bootstrap.install_omp()


@pytest.mark.parametrize("offset,value", [(0, 0), (4, 1), (5, 2), (18, 183)])
def test_omp_requires_a_matching_linux_elf(omp_release, monkeypatch, offset, value):
    binary, response = omp_release
    changed = bytearray(binary)
    changed[offset] = value
    monkeypatch.setattr(bootstrap, "OMP_RELEASES", {"x86_64": ("x64", len(changed), hashlib.sha256(changed).hexdigest(), 62)})
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *args, **kwargs: response(changed))
    with pytest.raises(bootstrap.BootstrapError, match="^omp-architecture$"):
        bootstrap.install_omp()


@pytest.mark.parametrize("stage", ["omp-version", "omp-help"])
def test_omp_rejected_execution_preserves_existing_binary(omp_release, worker_home, monkeypatch, stage):
    target = worker_home / ".local/bin/omp"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous binary")

    def run(argv, **kwargs):
        if kwargs["stage"] == stage:
            raise bootstrap.BootstrapError(stage)
        return subprocess.CompletedProcess(argv, 0, "omp/18.0.6\n", "")

    monkeypatch.setattr(bootstrap, "run", run)
    try:
        bootstrap.install_omp()
    except bootstrap.BootstrapError:
        pass
    assert target.read_bytes() == b"previous binary"


@pytest.mark.parametrize("output", ["omp/17.3.8", "", "18.0.6"])
def test_omp_requires_the_pinned_executable_version(omp_release, monkeypatch, output):
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""))
    with pytest.raises(bootstrap.BootstrapError, match="^omp-version$"):
        bootstrap.install_omp()


def test_omp_requires_the_native_flags_used_by_workers(omp_release, monkeypatch):
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "omp/18.0.6", ""))
    with pytest.raises(bootstrap.BootstrapError, match="^omp-help$"):
        bootstrap.install_omp()


def test_omp_failed_download_leaves_no_staging_executable(omp_release, worker_home, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("private network diagnostics")

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", failed)
    try:
        bootstrap.install_omp()
    except bootstrap.BootstrapError:
        pass
    assert list((worker_home / ".local/bin").iterdir()) == []


def test_omp_verification_receives_no_provider_credentials(omp_release, monkeypatch):
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-provider-token")

    def run(argv, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "omp/18.0.6" if argv[-1] == "--version"
                                           else "--no-tools --mode --approval-mode", "")

    monkeypatch.setattr(bootstrap, "run", run)
    bootstrap.install_omp()
    assert all("ANTHROPIC_API_KEY" not in env for env in calls)


def test_only_a_missing_requested_omp_is_installed(worker_home, monkeypatch, payload):
    payload["spec"]["harness"] = "omp"
    payload["profile"]["probe_enabled"] = False
    calls = []
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    monkeypatch.setattr(bootstrap, "worktree", lambda data, env: worker_home)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: command if calls else None)
    monkeypatch.setattr(bootstrap, "install_omp", lambda: calls.append("installed"))
    bootstrap.bootstrap(payload)
    assert calls == ["installed"]


@pytest.mark.parametrize("harness", ["claude", "codex", "omp"])
def test_installed_harnesses_do_not_download_omp(worker_home, monkeypatch, payload, harness):
    payload["spec"]["harness"] = harness
    payload["profile"]["probe_enabled"] = False
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    monkeypatch.setattr(bootstrap, "worktree", lambda data, env: worker_home)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: command)
    monkeypatch.setattr(bootstrap, "install_omp", lambda: pytest.fail("installed tools require no download"))
    assert bootstrap.bootstrap(payload)["status"] == "unverified"


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


def test_a_foreign_oneconv_launcher_cannot_shadow_the_managed_checkout(worker_home, monkeypatch, payload):
    path = worker_home / ".local/bin/one-conv"
    path.parent.mkdir(parents=True)
    path.write_text("a different launcher")
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    with pytest.raises(bootstrap.BootstrapError, match="launcher-conflict"):
        bootstrap.bootstrap(payload)


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


def test_the_actual_task_waits_for_bridge_publication_and_accounting(worker_home, monkeypatch, payload):
    stages = []
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {})
    monkeypatch.setattr(bootstrap, "worktree", lambda data, env: worker_home)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda command: command)
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, '{"type":"result","is_error":false,"result":"ONE_CONV_READY"}', ""))
    monkeypatch.setattr(bootstrap, "_bridge", lambda *args: stages.append("bridge"))
    monkeypatch.setattr(bootstrap, "_start", lambda *args: stages.append("native") or {"status": "registered", "address": "claude:resident"})
    monkeypatch.setattr(bootstrap, "_readiness", lambda *args: stages.append("publish") or {"status": "ready"})
    monkeypatch.setattr(bootstrap, "_collector", lambda *args: stages.append("collector") or True)
    monkeypatch.setattr(bootstrap, "_dispatch_task", lambda *args: stages.append("task") or "a" * 32)
    bootstrap.bootstrap(payload)
    assert stages == ["bridge", "native", "publish", "collector", "task"]


def test_secrets_and_prompt_are_direct_native_arguments(worker_home, monkeypatch):
    bootstrap.write_private(bootstrap.root() / "credentials.json", {"GH_TOKEN": "private-gh-token"})
    path = worker_home / "run.json"
    prompt = "$(touch /tmp/do-not-create) ; `private`"
    bootstrap.write_private(path, {"harness": "claude", "model": "selected-model", "prompt": prompt})
    calls = []
    monkeypatch.setattr(os, "execvpe", lambda executable, args, env: calls.append((executable, args, env)))
    bootstrap.run_native(path)
    assert calls[0][1] == ["claude", "--model", "selected-model", "--", prompt]


def test_cloud_claude_bypass_confirmation_is_per_invocation(worker_home, monkeypatch):
    bootstrap.write_private(bootstrap.root() / "credentials.json", {})
    path = worker_home / "run.json"
    bootstrap.write_private(path, {"harness": "claude", "model": None, "prompt": "task",
                                   "args": DEFAULT_PROFILE["harness_args"]["claude"]})
    calls = []
    monkeypatch.setattr(os, "execvpe", lambda executable, args, env: calls.append(args))
    bootstrap.run_native(path)
    assert calls == [["claude", "--dangerously-skip-permissions", "--settings",
                      '{"skipDangerousModePermissionPrompt":true}', "--", "task"]]


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


@pytest.mark.parametrize("harness,flag", [("claude", "--tools"), ("codex", "shell_tool"), ("omp", "--no-tools")])
def test_readiness_probes_disable_native_tools(harness, flag):
    assert flag in bootstrap.probe_command(harness, None)


@pytest.mark.parametrize("harness,output,expected", [
    ("claude", '{"type":"result","session_id":"probe-session"}', "claude:probe-session"),
    ("codex", '{"type":"thread.created","thread_id":"probe-thread"}', "codex:probe-thread"),
    ("omp", '{"type":"session","id":"probe-session"}', "omp:probe-session"),
])
def test_probe_sessions_keep_explicit_native_accounting_identity(harness, output, expected):
    assert bootstrap.probe_session(harness, output) == expected


@pytest.mark.parametrize("relative", ["Documents/workers/job-1/.claude/skills/review", ".codex/skills/review", ".omp/agent/skills/review"])
def test_selected_skills_are_linked_into_native_discovery(worker_home, monkeypatch, payload, relative):
    skill = worker_home / "Documents/claudine/.claude/skills/review"
    skill.mkdir(parents=True)
    payload["profile"]["skills"] = ["review"]
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "valid", ""))
    bootstrap.worktree(payload, {})
    assert (worker_home / relative).resolve() == skill.resolve()


def test_native_launcher_uses_profile_unattended_flags(worker_home, monkeypatch):
    bootstrap.write_private(bootstrap.root() / "credentials.json", {"GH_TOKEN": "private-gh-token"})
    path = worker_home / "run.json"
    bootstrap.write_private(path, {"harness": "claude", "model": None, "prompt": "task",
                                   "args": ["--dangerously-skip-permissions"]})
    calls = []
    monkeypatch.setattr(os, "execvpe", lambda executable, args, env: calls.append(args))
    bootstrap.run_native(path)
    assert calls[0] == ["claude", "--dangerously-skip-permissions", "--", "task"]


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
