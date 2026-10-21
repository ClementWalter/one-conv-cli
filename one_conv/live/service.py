"""Run the bridge as a macOS login service, so it starts at login and comes back after a crash.

launchd owns the process: `RunAtLoad` starts it when the user logs in, and
`KeepAlive.SuccessfulExit = false` restarts it only after a failure. A clean exit,
which is what `live serve --stop` causes, leaves it stopped until the next login.
The bridge reads its hub, node and token from its saved settings, so the service
definition holds no secret.
"""

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from . import bridge

LABEL = "dev.one-conv.bridge"
LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "one-conv"
# Seconds launchd waits between restarts, so a bridge failing at once (a refused token) cannot spin.
THROTTLE_SECONDS = 30


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


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


def installed() -> bool:
    return plist_path().is_file()


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    # Callers read the status: a bootout of a job that is not loaded fails harmlessly.
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30, check=False)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("The login service uses launchd, which only macOS has; run the bridge under tmux instead.")


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
    _require_macos()
    if not bridge.settings().get("hub"):
        raise RuntimeError("Run `one-conv live serve --hub ... --node ... --token-stdin` once before installing it.")
    notes = []
    if not installed() and bridge.status()["running"]:
        bridge.stop()
        if not _wait_until_stopped():
            raise RuntimeError("The running bridge did not stop; stop it with `one-conv live serve --stop` and retry.")
        notes.append("stopped the bridge that was running outside the login service")
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(definition(os.environ.get("PATH", "/usr/bin:/bin"))))
    _launchctl("bootout", f"{_domain()}/{LABEL}")  # Absent on a first install; reloads an edited job otherwise.
    loaded = _launchctl("bootstrap", _domain(), str(target))
    if loaded.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {loaded.stderr.strip() or loaded.returncode}")
    notes.append(f"installed {target}; the bridge starts at login and restarts after a crash")
    return notes


def uninstall() -> list[str]:
    """Stop the job and remove it; the bridge then runs only when started by hand."""
    _require_macos()
    if not installed():
        return ["the login service is not installed"]
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    plist_path().unlink()
    return [f"removed {plist_path()}; the bridge no longer starts at login"]
