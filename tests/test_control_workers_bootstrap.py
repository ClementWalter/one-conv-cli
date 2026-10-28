#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
"""The spawner box helper validates its private payload and builds the box from it, idempotently."""

import os
import subprocess
from pathlib import Path

import pytest

from one_conv.control import worker_bootstrap as bootstrap




@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    return tmp_path


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


def test_provider_failures_do_not_expose_diagnostics(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "private-token", "private-token"))
    with pytest.raises(bootstrap.BootstrapError, match="^github-login$"):
        bootstrap.run(["gh", "auth", "login"], stage="github-login")


def test_bootstrap_state_is_private(worker_home):
    path = worker_home / "state.json"
    bootstrap.write_private(path, {"private": "data"})
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.fixture
def spawner_payload():
    return {"operation": "spawner", "job_id": "spawner", "service_account": "private-op-token",
            "profile": {"repositories": ["ClementWalter/one-conv-cli"]},
            "git_user": {"name": "Ada Lovelace", "email": "ada@example.test"},
            "spawner": {"harness": "claude", "args": ["--dangerously-skip-permissions"], "priority": 0,
                        "env_file": "/run/ascii-secrets/env.sh"},
            "hub": {"url": "https://hub.example.test", "node": "spawner-box", "token": "private-hub-token"}}


@pytest.fixture
def provisioned(worker_home, monkeypatch, spawner_payload):
    """provision_spawner with every command recorded; lingering is off and the harness is logged in."""
    calls = []
    (worker_home / "Documents/one-conv-cli/bin").mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "prepare_repositories", lambda data: {"PATH": os.environ["PATH"]})
    monkeypatch.setattr(bootstrap, "install_uv", lambda: worker_home / ".local/bin/uv")
    monkeypatch.setattr(bootstrap, "_hub_settings", lambda data: None)
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kw: calls.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "no\n" if argv[0] == "loginctl" else "abc123\n", ""))
    from one_conv.live import spawn
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: None)
    monkeypatch.setattr(spawn, "environment", lambda env_file=None: {})
    return calls


def launched(calls, *words):
    """The recorded one-conv command that runs `live <words>`."""
    return next(argv for argv in calls if argv[4:5 + len(words)] == ["live", *words])


def test_a_spawner_payload_is_accepted(spawner_payload):
    assert bootstrap.validate_payload(spawner_payload) is spawner_payload


@pytest.mark.parametrize("field, value", [
    ("git_user", {"name": "Ada\nEvil", "email": "ada@example.test"}),
    ("git_user", None),
    ("spawner", {"harness": "aider", "args": [], "priority": 0, "env_file": None}),
    ("spawner", {"harness": "claude", "args": [], "priority": 0, "env_file": "relative/env.sh"}),
    ("spawner", {"harness": "claude", "args": [], "priority": 5000, "env_file": None}),
    ("profile", {"repositories": ["ClementWalter/one-conv-cli"], "revisions": {"ClementWalter/one-conv-cli": "d" * 40}}),
    ("profile", {"repositories": ["ClementWalter/other"]}),
    ("operation", "start"),
    ("service_account", "two words"),
])
def test_bad_spawner_payloads_are_refused(spawner_payload, field, value):
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.validate_payload({**spawner_payload, field: value})


def test_a_spawner_follows_main(provisioned, spawner_payload):
    bootstrap.provision_spawner(spawner_payload)
    assert provisioned[:2] == [["git", "-C", str(Path.home() / "Documents/one-conv-cli"), "checkout", "-q", "main"],
                               ["git", "-C", str(Path.home() / "Documents/one-conv-cli"), "pull", "-q", "--ff-only"]]


def test_a_spawner_commits_as_the_configured_user(provisioned, spawner_payload):
    bootstrap.provision_spawner(spawner_payload)
    assert ["git", "config", "--global", "user.email", "ada@example.test"] in provisioned


def test_a_spawner_reads_its_credentials_from_the_env_file(provisioned, spawner_payload):
    bootstrap.provision_spawner(spawner_payload)
    argv = launched(provisioned, "spawner")
    assert argv[argv.index("--env-file") + 1] == "/run/ascii-secrets/env.sh"


def test_a_spawner_passes_its_harness_arguments(provisioned, spawner_payload):
    bootstrap.provision_spawner(spawner_payload)
    assert "--arg=--dangerously-skip-permissions" in launched(provisioned, "spawner")


def test_a_spawner_installs_the_bridge_service(provisioned, spawner_payload):
    bootstrap.provision_spawner(spawner_payload)
    assert launched(provisioned, "serve")[-2:] == ["--install", "--yes"]


def test_a_claude_spawner_installs_claudes_native_build(provisioned, spawner_payload):
    bootstrap.provision_spawner(spawner_payload)
    assert ["claude", "install", "latest"] in provisioned


def test_a_claude_spawner_keeps_a_native_build_already_there(provisioned, spawner_payload):
    native = Path.home() / ".local/bin/claude"
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_text("")
    bootstrap.provision_spawner(spawner_payload)
    assert ["claude", "install", "latest"] not in provisioned


def test_a_codex_spawner_installs_no_claude(provisioned, spawner_payload):
    bootstrap.provision_spawner({**spawner_payload, "spawner": {**spawner_payload["spawner"], "harness": "codex"}})
    assert ["claude", "install", "latest"] not in provisioned


def test_a_spawner_turns_lingering_on(provisioned, spawner_payload):
    bootstrap.provision_spawner(spawner_payload)
    assert any(argv[:4] == ["sudo", "-n", "loginctl", "enable-linger"] for argv in provisioned)


def test_a_provisioned_spawner_reports_its_revision(provisioned, spawner_payload):
    assert bootstrap.provision_spawner(spawner_payload) == {"status": "ready", "revision": "abc123"}


def test_a_logged_out_spawner_needs_auth(provisioned, spawner_payload, monkeypatch):
    from one_conv.live import spawn
    monkeypatch.setattr(spawn, "preflight", lambda harness, env=None: (_ for _ in ()).throw(spawn.SpawnError("no")))
    assert bootstrap.provision_spawner(spawner_payload)["status"] == "auth_needed"


def test_the_images_uv_is_used(worker_home, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/local/bin/uv")
    monkeypatch.setattr(bootstrap, "run", lambda *args, **kwargs: pytest.fail("an installed uv is not replaced"))
    assert bootstrap.install_uv() == Path("/usr/local/bin/uv")


def test_a_missing_uv_is_installed_from_the_pinned_installer(worker_home, monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setattr(bootstrap, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    bootstrap.install_uv()
    assert calls[0][-1] == f"https://astral.sh/uv/{bootstrap.UV_VERSION}/install.sh"
