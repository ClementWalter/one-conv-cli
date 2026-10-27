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
"""Native credential lifecycle keeps passive reads noninteractive and identity stable."""

from pathlib import Path
import subprocess

import pytest

from one_conv import native_auth


@pytest.fixture
def helper(monkeypatch, tmp_path):
    path = tmp_path / "OneConv" / "credential-helper-v1"
    monkeypatch.setattr(native_auth.sys, "platform", "darwin")
    monkeypatch.setattr(native_auth, "helper_path", lambda: path)
    return path


@pytest.fixture
def installed(helper):
    helper.parent.mkdir(mode=0o700)
    helper.write_bytes(b"synthetic executable")
    helper.chmod(0o700)
    return helper


def test_missing_passive_helper_does_not_install(monkeypatch, helper):
    def forbidden():
        raise AssertionError("Passive read must not install")
    monkeypatch.setattr(native_auth, "ensure_helper", forbidden)
    assert native_auth.read_secret("Chrome Safe Storage") is None


def test_passive_read_uses_read_mode(monkeypatch, installed):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 3, b"", b"denied")
    monkeypatch.setattr(native_auth.subprocess, "run", run)
    native_auth.read_secret("Chrome Safe Storage")
    assert calls == [([str(installed), "read", "Chrome Safe Storage"], {"capture_output": True, "timeout": 10})]


def test_authorization_explicitly_selects_interactive_mode(monkeypatch, installed):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 3, b"", b"")
    monkeypatch.setattr(native_auth.subprocess, "run", run)
    native_auth.read_secret("Chrome Safe Storage", authorize=True)
    assert calls == [([str(installed), "authorize", "Chrome Safe Storage"], 600)]


def test_returns_secret_only_on_success(monkeypatch, installed):
    monkeypatch.setattr(native_auth.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"synthetic", b""))
    assert native_auth.read_secret("Chrome Safe Storage") == b"synthetic"


def test_failed_helper_stdout_is_discarded(monkeypatch, installed):
    monkeypatch.setattr(native_auth.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 3, b"untrusted output", b""))
    assert native_auth.read_secret("Chrome Safe Storage") is None


def test_existing_helper_is_never_rebuilt(monkeypatch, installed):
    def forbidden(*args, **kwargs):
        raise AssertionError("Existing identity must remain stable")
    monkeypatch.setattr(native_auth.subprocess, "run", forbidden)
    assert native_auth.ensure_helper() == installed


def test_symlink_helper_is_not_executed(helper):
    helper.parent.mkdir()
    helper.symlink_to("missing-target")
    assert native_auth.read_secret("Chrome Safe Storage") is None


def test_publicly_readable_helper_is_not_executed(installed):
    installed.chmod(0o755)
    assert native_auth.read_secret("Chrome Safe Storage") is None


def test_unknown_service_is_rejected(helper):
    with pytest.raises(ValueError, match="Unsupported"):
        native_auth.read_secret("arbitrary password")


def test_nonmac_read_is_unavailable(monkeypatch):
    monkeypatch.setattr(native_auth.sys, "platform", "linux")
    assert native_auth.read_secret("Chrome Safe Storage") is None


def test_timeout_returns_no_credential(monkeypatch, installed):
    def fail(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(native_auth.subprocess, "run", fail)
    assert native_auth.read_secret("Chrome Safe Storage") is None


def test_explicit_install_uses_optimized_signed_build(monkeypatch, helper, tmp_path):
    source = tmp_path / "CredentialHelper.swift"
    source.write_text("// Synthetic compilation input")
    monkeypatch.setattr(native_auth, "SOURCE", source)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "swiftc" in command:
            Path(command[-1]).write_bytes(b"synthetic executable")
        return subprocess.CompletedProcess(command, 0, b"", b"")
    monkeypatch.setattr(native_auth.subprocess, "run", run)
    native_auth.ensure_helper()
    assert (calls[0][2], calls[1][3], calls[2][2]) == ("-O", "-", "--strict")


def test_build_failure_is_sanitized(monkeypatch, helper, tmp_path):
    source = tmp_path / "CredentialHelper.swift"
    source.write_text("// Synthetic input")
    monkeypatch.setattr(native_auth, "SOURCE", source)
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=b"private compiler detail")
    monkeypatch.setattr(native_auth.subprocess, "run", fail)
    with pytest.raises(native_auth.NativeAuthError, match="Could not build"):
        native_auth.ensure_helper()
