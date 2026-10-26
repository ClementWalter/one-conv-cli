"""Run the bridge as a login service, so it starts on its own and comes back after a crash.

On macOS launchd owns the process: `RunAtLoad` starts it when the user logs in, and
`KeepAlive.SuccessfulExit = false` restarts it only after a failure. On Linux a systemd
user unit does the same with `Restart=on-failure`, and starts at boot when the user has
lingering enabled. A clean exit, which is what `live serve --stop` causes, leaves it
stopped. The bridge reads its hub, node and token from its saved settings, and spawned
sessions read credentials through `live spawner --env-file`, so the service definition
holds no secret.
"""

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from . import bridge

LABEL = "dev.one-conv.bridge"
# The cloud controller restarts the box's bridge by this name.
UNIT = "one-conv-bridge.service"
LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "one-conv"
# Seconds launchd waits between restarts, so a bridge failing at once (a refused token) cannot spin.
THROTTLE_SECONDS = 30


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def path() -> Path:
    """This system's service definition file."""
    return plist_path() if sys.platform == "darwin" else unit_path()


def _unit_value(value: str) -> str:
    """A path quoted for systemd: `%` starts a specifier, so it is doubled."""
    if any(ord(char) < 32 for char in value):
        raise RuntimeError(f"cannot write a control character into the service unit: {value!r}")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit(path_env: str) -> str:
    """The systemd user unit. PATH is the installer's, so the bridge finds uv, tmux and the agent harnesses."""
    return "\n".join([
        "[Unit]",
        "Description=one-conv live bridge and spawner",
        "Wants=network-online.target",
        "After=network-online.target",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={str(LAUNCHER.parents[1]).replace('%', '%%')}",
        f"Environment={_unit_value('PATH=' + path_env)}",
        f"ExecStart={_unit_value(str(LAUNCHER))} live serve",
        "Restart=on-failure",
        f"RestartSec={THROTTLE_SECONDS}",
        # Sessions the bridge started live in tmux; restarting the bridge must leave them running.
        "KillMode=process",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def definition(path_env: str) -> dict:
    """The launchd job. PATH is the installer's, so the bridge finds uv, tmux and the agent harnesses."""
    log = bridge.bridge_dir() / "serve.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(LAUNCHER), "live", "serve"],
        "WorkingDirectory": str(LAUNCHER.parents[1]),
        "EnvironmentVariables": {"PATH": path_env},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_SECONDS,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def service_path(path_env: str) -> str:
    """The installer's PATH without the launcher's private uv environment, whose folder changes with
    its dependencies and whose python would shadow the system one in spawned sessions."""
    private = str(Path(sys.executable).parent)
    kept = []
    for entry in path_env.split(os.pathsep):
        if entry and entry != private and entry not in kept:
            kept.append(entry)
    return os.pathsep.join(kept)


def installed() -> bool:
    return path().is_file()


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    # Callers read the status: a bootout of a job that is not loaded fails harmlessly.
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30, check=False)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False)


def _require_supported() -> None:
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise RuntimeError("The login service needs launchd (macOS) or systemd (Linux); run the bridge under tmux instead.")


def _wait_until_stopped(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while bridge.status()["running"]:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def install() -> list[str]:
    """Write the job and start it. A bridge already running outside launchd is stopped first,
    since only one bridge per machine can hold the serve lock."""
    _require_supported()
    if not bridge.settings().get("hub"):
        raise RuntimeError("Run `one-conv live serve --hub ... --node ... --token-stdin` once before installing it.")
    notes = []
    if not installed() and bridge.status()["running"]:
        bridge.stop()
        if not _wait_until_stopped():
            raise RuntimeError("The running bridge did not stop; stop it with `one-conv live serve --stop` and retry.")
        notes.append("stopped the bridge that was running outside the login service")
    path_env = service_path(os.environ.get("PATH", "/usr/bin:/bin"))
    if sys.platform != "darwin":
        return notes + _install_systemd(path_env)
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(definition(path_env)))
    _launchctl("bootout", f"{_domain()}/{LABEL}")  # Absent on a first install; reloads an edited job otherwise.
    loaded = _launchctl("bootstrap", _domain(), str(target))
    if loaded.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {loaded.stderr.strip() or loaded.returncode}")
    notes.append(f"installed {target}; the bridge starts at login and restarts after a crash")
    return notes


def _install_systemd(path_env: str) -> list[str]:
    """Write the unit, enable it, and (re)start it; tmux sessions survive the restart (KillMode=process)."""
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit(path_env))
    for args in (("daemon-reload",), ("enable", UNIT), ("restart", UNIT)):
        result = _systemctl(*args)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip() or result.returncode}")
    notes = [f"installed {target}; the bridge restarts after a crash"]
    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                            capture_output=True, text=True, timeout=30, check=False).stdout.strip()
    notes.append("it starts at boot (lingering is on)" if linger == "yes"
                 else "it starts at login; run `loginctl enable-linger` to start it at boot")
    return notes


def uninstall() -> list[str]:
    """Stop the job and remove it; the bridge then runs only when started by hand."""
    _require_supported()
    if not installed():
        return ["the login service is not installed"]
    if sys.platform == "darwin":
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    else:
        # `disable --now` stops the bridge; its tmux sessions keep running.
        _systemctl("disable", "--now", UNIT)
    path().unlink()
    if sys.platform != "darwin":
        _systemctl("daemon-reload")
    return [f"removed {path()}; the bridge no longer starts at login"]
