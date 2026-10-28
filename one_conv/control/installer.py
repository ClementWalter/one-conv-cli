#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Install a pinned cloud controller from a private, disposable JSON payload.

The prepared Linux worker must already have GitHub authentication and clones in
~/Documents. Credentials travel through stdin or private files; child diagnostics
stay captured. Pairing devices remains a separate operator action.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
UNIT = "one-conv-control.service"
REPOSITORY = "https://github.com/ClementWalter/one-conv-cli.git"
BOAT_URL = "https://boat.dev/api/boat/cli/download?platform=linux-x64&channel=prod"
# sqlalchemy and pydantic back the conversation archive the controller serves to every node.
DEPENDENCIES = ("click==8.3.3", "starlette==1.8.0", "uvicorn==0.54.0", "sqlalchemy==2.1.4", "pydantic==2.12.5")
PAYLOAD_LIMIT = 256_000
DOWNLOAD_LIMIT = 256 * 1024 * 1024
STAGES = frozenset({
    "payload", "payload-cleanup", "platform", "repository-origin", "repository-fetch", "repository-checkout", "repository-head",
    "python-environment", "python-dependencies", "python-path", "boat-download", "boat-login", "runtime-imports",
    "service-unit", "service-manager", "service-user", "service-linger", "service-linger-check", "service-install",
    "service-reload", "service-start", "service-restart", "service-active", "service-enabled", "service-health",
})


class InstallationError(RuntimeError):
    """Expose a fixed operation label without provider output or credential values."""

    def __init__(self, stage):
        self.stage = stage if isinstance(stage, str) and stage in STAGES else "installation"
        super().__init__(self.stage)


def run(argv, *, stage, input=None, check=True, env=None):
    """Capture subprocess output because authentication tools can echo credentials."""
    try:
        result = subprocess.run(argv, input=input, capture_output=True, text=True,
                                check=False, timeout=180, env=env)
    except (OSError, subprocess.SubprocessError):
        raise InstallationError(stage) from None
    if check and result.returncode:
        raise InstallationError(stage)
    return result


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def validate_payload(value):
    """Keep the credential envelope small, explicit and independent of CLI parsing."""
    if not isinstance(value, dict) or set(value) != {"revision", "public_origin", "boat_token", "service_account", "worker_config"}:
        raise InstallationError("payload")
    if not isinstance(value["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["revision"]):
        raise InstallationError("payload")
    for name in ("boat_token", "service_account"):
        token = value[name]
        if not isinstance(token, str) or not 1 <= len(token) <= 32_000 or any(not 33 <= ord(c) <= 126 for c in token):
            raise InstallationError("payload")
    origin = value["public_origin"]
    if not isinstance(origin, str) or len(origin) > 2048:
        raise InstallationError("payload")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                 and parsed.netloc.isascii() and parsed.netloc == parsed.netloc.lower()
                 and not parsed.netloc.endswith(":") and not parsed.hostname.endswith(".")
                 and not any(c.isspace() or ord(c) < 32 for c in origin))
    except ValueError:
        valid = False
    if not valid or not isinstance(value["worker_config"], dict):
        raise InstallationError("payload")
    authority = parsed.netloc
    if port == 443:
        authority = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return {**value, "public_origin": "https://" + authority}


def read_payload(path):
    """Delete the disposable credential envelope even when validation fails."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o777 != 0o600 or metadata.st_size > PAYLOAD_LIMIT):
                raise InstallationError("payload")
            value = json.loads(stream.read(PAYLOAD_LIMIT + 1), object_pairs_hook=_object, parse_constant=_constant)
        return validate_payload(value)
    except (OSError, ValueError, TypeError, RecursionError):
        raise InstallationError("payload") from None
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            raise InstallationError("payload-cleanup") from None


def private_file(path, content, mode=0o600):
    """Replace state atomically without following a preexisting destination symlink."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".control-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def download_boat(target):
    """Accept only a bounded Linux x64 executable from the official HTTPS endpoint."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(BOAT_URL, timeout=60) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise InstallationError("boat-download")
            binary = response.read(DOWNLOAD_LIMIT + 1)
        if (len(binary) > DOWNLOAD_LIMIT or len(binary) < 64 or binary[:6] != b"\x7fELF\x02\x01"
                or binary[18:20] != b"\x3e\x00"):
            raise InstallationError("boat-download")
        private_file(target, binary, 0o700)
    except (OSError, ValueError):
        raise InstallationError("boat-download") from None


def unit_argument(value):
    """Escape systemd specifiers as well as quoting, without invoking a shell."""
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise InstallationError("service-unit")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_path(value):
    """Scalar path settings preserve interior spaces and expand only escaped specifiers."""
    value = str(value)
    if not Path(value).is_absolute() or value != value.strip() or any(ord(c) < 32 for c in value):
        raise InstallationError("service-unit")
    return value.replace("%", "%%")


def service_unit(python, repository, state, config, origin, home, *, user=None):
    """The service contains paths and public configuration, never credential values."""
    command = [python, "-O", "-m", "one_conv.control", "--directory", state, "serve", "--origin", origin,
               "--host", "0.0.0.0", "--port", "8790", "--config", config]
    identity = f"User={user}\n" if user else ""
    return ("[Unit]\nDescription=One Conv personal cloud workspace\nAfter=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nType=simple\n{identity}WorkingDirectory={unit_path(repository)}\n"
            f"Environment={unit_argument('HOME=' + str(home))}\n"
            f"Environment={unit_argument('PATH=' + str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin')}\n"
            f"ExecStart={' '.join(unit_argument(arg) for arg in command)}\n"
            "Restart=always\nRestartSec=3\nTimeoutStopSec=30\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n\n"
            "[Install]\nWantedBy=" + ("multi-user.target\n" if user else "default.target\n"))


def start_service(home, content, runner=run):
    """Use a user manager when available, otherwise install for the current system user."""
    user_manager = runner(["systemctl", "--user", "show-environment"], stage="service-manager", check=False).returncode == 0
    username = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*\$?", username):
        raise InstallationError("service-user")
    prefix = [] if os.getuid() == 0 else ["sudo", "-n"]
    if user_manager:
        try:
            enabled = runner([*prefix, "loginctl", "enable-linger", username], stage="service-linger", check=False)
            linger = runner(["loginctl", "show-user", username, "--property=Linger", "--value"],
                            stage="service-linger-check", check=False)
            user_manager = enabled.returncode == linger.returncode == 0 and linger.stdout.strip() == "yes"
        except InstallationError:
            user_manager = False
    if user_manager:
        path = home / ".config/systemd/user" / UNIT
        private_file(path, content(None).encode())
        command = ["systemctl", "--user"]
    else:
        source = home / ".local/share/one-conv-control" / UNIT
        private_file(source, content(username).encode())
        runner([*prefix, "install", "-m", "0644", str(source), "/etc/systemd/system/" + UNIT], stage="service-install")
        command = [*prefix, "systemctl"]
    runner([*command, "daemon-reload"], stage="service-reload")
    runner([*command, "enable", "--now", UNIT], stage="service-start")
    runner([*command, "restart", UNIT], stage="service-restart")
    runner([*command, "is-active", "--quiet", UNIT], stage="service-active")
    runner([*command, "is-enabled", "--quiet", UNIT], stage="service-enabled")


def check_health(origin, *, port=8790, seconds=30):
    """Probe the loopback service with its canonical Host and no browser credentials."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + seconds
    while True:
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"Host": urlsplit(origin).netloc})
            with opener.open(request, timeout=3) as response:
                if response.status == 200 and json.loads(response.read(1024)) == {"ok": True}:
                    return True
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise InstallationError("service-health")
        time.sleep(1)


def install(payload_path, *, home=None, runner=run, downloader=download_boat, health=check_health):
    """Provision only this prepared host; no sandbox or API key is created here."""
    payload = read_payload(payload_path)
    previous = os.umask(0o077)
    try:
        return _install(payload, home, runner, downloader, health)
    finally:
        os.umask(previous)


def _install(payload, home, runner, downloader, health):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise InstallationError("platform")
    home = Path.home() if home is None else Path(home)
    repository = home / "Documents/one-conv-cli"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    origin = runner(["git", "-C", str(repository), "remote", "get-url", "origin"], stage="repository-origin", env=env)
    if origin.stdout.strip() != REPOSITORY:
        raise InstallationError("repository-origin")
    runner(["git", "-C", str(repository), "fetch", "origin", payload["revision"]], stage="repository-fetch", env=env)
    runner(["git", "-C", str(repository), "checkout", "--detach", payload["revision"]], stage="repository-checkout", env=env)
    head = runner(["git", "-C", str(repository), "rev-parse", "HEAD"], stage="repository-head", env=env)
    if head.stdout.strip() != payload["revision"]:
        raise InstallationError("repository-head")
    base, state = home / ".local/share/one-conv-control", home / ".config/one-conv/control"
    python = base / "venv/bin/python"
    runner(["uv", "venv", "--no-project", "--clear", "--python", sys.executable, str(base / "venv")], stage="python-environment")
    runner(["uv", "pip", "install", "--python", str(python), *DEPENDENCIES], stage="python-dependencies")
    purelib = runner([str(python), "-O", "-c", "import sys,sysconfig; sys.stdout.write(sysconfig.get_path('purelib'))"], stage="python-path").stdout.strip()
    site = Path(purelib)
    if not site.is_absolute() or not site.is_relative_to(base / "venv"):
        raise InstallationError("python-path")
    private_file(site / "one_conv.pth", (str(repository) + "\n").encode())
    boat = base / "bin/box"
    downloader(boat)
    runner([str(boat), "login", "--key-stdin", "--json", "--no-update"], stage="boat-login", input=payload["boat_token"] + "\n")
    account, config = state / "service-account.json", state / "worker-config.json"
    private_file(account, json.dumps({"service_account": payload["service_account"]}).encode())
    settings = {**payload["worker_config"], "boat_executable": str(boat), "service_account_file": str(account)}
    private_file(config, json.dumps(settings).encode())
    source = ("import json,sys; from pathlib import Path; from one_conv.control.runtime import application; "
              "application(Path(sys.argv[1]),sys.argv[2],worker_config=json.loads(Path(sys.argv[3]).read_text()))")
    runner([str(python), "-O", "-c", source, str(state), payload["public_origin"], str(config)], stage="runtime-imports")
    runner([str(python), "-O", "-c", "import archive.server, archive.store"], stage="runtime-imports")
    def content(user):
        return service_unit(python, repository, state, config, payload["public_origin"], home, user=user)

    start_service(home, content, runner)
    health(payload["public_origin"])
    return {"revision_verified": True, "runtime_imports": True, "boat_login": True,
            "service_enabled": True, "healthy": True, "unit": UNIT}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path, help="Private mode-0600 JSON envelope; always removed after reading.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = install(args.payload)
    except InstallationError as error:
        log.error("Cloud installation is not confirmed (stage=%s); provider diagnostics are withheld.", error.stage)
        return 1
    except (OSError, ValueError):
        log.error("Cloud installation is not confirmed (stage=installation); provider diagnostics are withheld.")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
