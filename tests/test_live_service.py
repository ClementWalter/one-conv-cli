"""The bridge's macOS login service: launchd job shape, install and removal, without touching the real launchd."""

import plistlib
import subprocess

import pytest
from click.testing import CliRunner

from one_conv.live import bridge, service
from one_conv.live.cli import build_live_group


@pytest.fixture
def macos(monkeypatch, tmp_path):
    """A macOS home in tmp_path with saved bridge settings; launchctl calls are recorded, not run."""
    calls = []
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(service, "_launchctl",
                        lambda *args: calls.append(list(args)) or subprocess.CompletedProcess(args, 0, "", ""))
    bridge.save_settings("https://hub.example.test", "laptop", 8789, "synthetic-token")
    return calls


def serve(*args):
    return CliRunner().invoke(build_live_group(["one-conv"]), ["serve", *args])


def test_job_runs_the_launcher_with_saved_settings():
    assert service.definition("/usr/bin")["ProgramArguments"] == [str(service.LAUNCHER), "live", "serve"]


def test_job_restarts_only_after_a_failure():
    assert service.definition("/usr/bin")["KeepAlive"] == {"SuccessfulExit": False}


def test_job_starts_at_login():
    assert service.definition("/usr/bin")["RunAtLoad"] is True


def test_job_keeps_the_installer_path():
    assert service.definition("/opt/tools/bin:/usr/bin")["EnvironmentVariables"] == {"PATH": "/opt/tools/bin:/usr/bin"}


def test_job_holds_no_token():
    assert "synthetic-token" not in plistlib.dumps(service.definition("/usr/bin")).decode()


def test_install_without_yes_writes_nothing(macos):
    serve("--install")
    assert not service.plist_path().exists()


def test_install_writes_the_job(macos):
    serve("--install", "--yes")
    assert plistlib.loads(service.plist_path().read_bytes())["Label"] == service.LABEL


def test_install_loads_the_job(macos):
    serve("--install", "--yes")
    assert macos[-1] == ["bootstrap", service._domain(), str(service.plist_path())]


def test_install_stops_a_bridge_running_outside_launchd(macos, monkeypatch):
    running = [True]
    monkeypatch.setattr(bridge, "status", lambda: {"running": running[0]})
    monkeypatch.setattr(bridge, "stop", lambda: running.__setitem__(0, False) or True)
    serve("--install", "--yes")
    assert running == [False]


def test_install_needs_saved_bridge_settings(macos):
    (bridge.bridge_dir() / "serve.json").unlink()
    assert "--token-stdin` once" in serve("--install", "--yes").output


def test_install_refuses_other_systems(macos, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert "only macOS" in serve("--install", "--yes").output


def test_failed_bootstrap_is_reported(macos, monkeypatch):
    monkeypatch.setattr(service, "_launchctl", lambda *args: subprocess.CompletedProcess(args, 5, "", "Input/output error"))
    assert "launchctl bootstrap failed: Input/output error" in serve("--install", "--yes").output


def test_uninstall_removes_the_job(macos):
    serve("--install", "--yes")
    serve("--uninstall", "--yes")
    assert not service.plist_path().exists()


def test_uninstall_unloads_the_job(macos):
    serve("--install", "--yes")
    serve("--uninstall", "--yes")
    assert macos[-1] == ["bootout", f"{service._domain()}/{service.LABEL}"]


def test_status_says_whether_it_starts_at_login(macos):
    serve("--install", "--yes")
    assert "starts at login: yes" in serve("--status").output
