"""The bridge's login service: launchd job and systemd unit shape, install and removal, without touching either."""

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
    # `print` fails as for a job launchd has let go of.
    monkeypatch.setattr(service, "_launchctl", lambda *args: calls.append(list(args))
                        or subprocess.CompletedProcess(args, 113 if args[0] == "print" else 0, "", ""))
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
    monkeypatch.setattr(service.sys, "platform", "win32")
    assert "launchd (macOS) or systemd (Linux)" in serve("--install", "--yes").output


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


# --- Linux: systemd user unit ------------------------------------------------------------------


@pytest.fixture
def linux(monkeypatch, tmp_path):
    """A Linux home in tmp_path with saved bridge settings; systemctl and loginctl calls are recorded, not run."""
    calls = []
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(service, "_systemctl",
                        lambda *args: calls.append(list(args)) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(service.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "yes\n", ""))
    bridge.save_settings("https://hub.example.test", "box", 8789, "synthetic-token")
    return calls


def test_unit_runs_the_launcher():
    assert f'ExecStart="{service.LAUNCHER}" live serve' in service.unit("/usr/bin").splitlines()


def test_unit_restarts_only_after_a_failure():
    assert "Restart=on-failure" in service.unit("/usr/bin").splitlines()


def test_unit_leaves_spawned_tmux_sessions_running():
    assert "KillMode=process" in service.unit("/usr/bin").splitlines()


def test_unit_keeps_the_installer_path():
    assert 'Environment="PATH=/opt/tools/bin:/usr/bin"' in service.unit("/opt/tools/bin:/usr/bin").splitlines()


def test_unit_doubles_a_percent_sign():
    assert 'Environment="PATH=/opt/100%%/bin"' in service.unit("/opt/100%/bin").splitlines()


def test_unit_holds_no_token(linux):
    serve("--install", "--yes")
    assert "synthetic-token" not in service.unit_path().read_text()


def test_linux_install_writes_the_unit(linux):
    serve("--install", "--yes")
    assert service.unit_path().read_text() == service.unit(service.service_path(service.os.environ.get("PATH", "/usr/bin:/bin")))


def test_linux_install_enables_and_restarts_the_unit(linux):
    serve("--install", "--yes")
    assert linux[:3] == [["daemon-reload"], ["enable", service.UNIT], ["restart", service.UNIT]]


def test_linux_install_says_it_starts_at_boot_with_lingering(linux):
    assert "starts at boot" in serve("--install", "--yes").output


def test_failed_systemctl_is_reported(linux, monkeypatch):
    monkeypatch.setattr(service, "_systemctl", lambda *args: subprocess.CompletedProcess(args, 1, "", "no bus"))
    assert "systemctl --user daemon-reload failed: no bus" in serve("--install", "--yes").output


def test_linux_uninstall_removes_the_unit(linux):
    serve("--install", "--yes")
    serve("--uninstall", "--yes")
    assert not service.unit_path().exists()


def test_linux_uninstall_stops_the_unit(linux):
    serve("--install", "--yes")
    serve("--uninstall", "--yes")
    assert ["disable", "--now", service.UNIT] in linux


def test_service_path_drops_the_launchers_environment():
    private = str(service.Path(service.sys.executable).parent)
    assert service.service_path(f"{private}:/usr/bin") == "/usr/bin"


def test_service_path_keeps_the_first_of_repeated_entries():
    assert service.service_path("/a:/b:/a") == "/a:/b"


def test_install_loads_the_job_only_once_launchd_let_go_of_it(macos, monkeypatch):
    held = iter([0, 0, 113])
    calls = []
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(service, "_launchctl", lambda *args: calls.append(args[0])
                        or subprocess.CompletedProcess(args, next(held) if args[0] == "print" else 0, "", ""))
    serve("--install", "--yes")
    assert calls == ["bootout", "print", "print", "print", "bootstrap"]
