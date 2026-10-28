#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Make a Boat box the cloud spawner node from a private JSON payload.

Boat runs this dependency-free helper with fixed arguments. Secrets remain in
private files or descendant environments, never in a shell command.
"""

from __future__ import annotations

import fcntl
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
OP_VERSION = "2.32.0"
GITHUB_REF = "op://Claudine/GitHub gh token/credential"
UV_VERSION = "0.13.5"
SPAWNER_HARNESSES = ("claude", "codex", "omp")
STAGE = "payload"




class BootstrapError(RuntimeError):
    """A fixed bootstrap stage safe to return to the controller."""


def run(argv, *, stage, env=None, input=None, timeout=180, check=True, cwd=None):
    """Capture private diagnostics while retaining only the operation's stage."""
    global STAGE
    STAGE = stage
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                check=False, env=env, input=input, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        raise BootstrapError(stage) from None
    if check and result.returncode:
        raise BootstrapError(stage)
    return result


def write_private(path, data):
    """Flush a private replacement before launching a process that consumes it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def root():
    return Path.home() / ".config/one-conv/workers"


def read_private(path):
    """Consume a payload before using its credentials so cleanup survives later errors."""
    try:
        path.chmod(0o600)
        data = json.loads(path.read_text())
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise BootstrapError("payload")
    return data


def _repositories(repos):
    return (isinstance(repos, list) and repos and all(isinstance(repo, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repo)
            and repo.split("/")[1] not in (".", "..") and not repo.endswith(".git") for repo in repos)
            and len({repo.split("/")[1].lower() for repo in repos}) == len(repos)
            and "ClementWalter/one-conv-cli" in repos)


def _text(value, limit=256):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(char) < 32 for char in value)


def _hub(hub):
    if not isinstance(hub, dict) or not isinstance(hub.get("node"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", hub["node"]):
        return False
    url = urlsplit(hub.get("url", ""))
    return not (url.username or url.password or not url.hostname or url.scheme not in ("https", "http"))


def _secret(token):
    return isinstance(token, str) and token and not any(char.isspace() for char in token) and "\x00" not in token


def validate_spawner(data):
    """The spawner box's settings: what it clones, who it commits as, and how its bridge starts sessions."""
    spawner, profile, git_user = data.get("spawner"), data.get("profile"), data.get("git_user")
    if (not isinstance(profile, dict) or not _repositories(profile.get("repositories"))
            or set(profile) != {"repositories"}
            or not _hub(data.get("hub")) or not _secret(data.get("service_account")) or not _secret(data["hub"].get("token"))
            or not isinstance(git_user, dict) or not _text(git_user.get("name")) or not _text(git_user.get("email"))
            or not isinstance(spawner, dict) or spawner.get("harness") not in SPAWNER_HARNESSES
            or type(spawner.get("priority")) is not int or not -1000 <= spawner["priority"] <= 1000
            or not isinstance(spawner.get("args"), list) or len(spawner["args"]) > 32
            or not all(_text(arg, 1024) for arg in spawner["args"])
            or not (spawner.get("env_file") is None or _text(spawner["env_file"], 1024) and spawner["env_file"].startswith("/"))):
        raise BootstrapError("payload")
    return data


def validate_payload(data):
    """Validate the operation before any credential read, clone or install."""
    if not isinstance(data, dict) or data.get("operation") != "spawner":
        raise BootstrapError("payload")
    return validate_spawner(data)


def install_op():
    """Install only the pinned official binary, without archive path extraction."""
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if arch is None:
        raise BootstrapError("op-architecture")
    target = Path.home() / ".local/bin/op"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or run([str(target), "--version"], stage="op-version").stdout.strip() != OP_VERSION:
        url = f"https://cache.agilebits.com/dist/1P/op2/pkg/v{OP_VERSION}/op_linux_{arch}_v{OP_VERSION}.zip"
        with urllib.request.urlopen(url, timeout=60) as response, zipfile.ZipFile(io.BytesIO(response.read())) as archive:
            target.write_bytes(archive.read("op"))
        target.chmod(0o755)
    return target


def prepare_repositories(data):
    """Read the narrow GitHub credential and verify each selected clone, left on its own branch."""
    profile = data["profile"]
    op = install_op()
    env = {**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": data["service_account"]}
    token = run([str(op), "read", GITHUB_REF], stage="github-vault", env=env).stdout.strip()
    if not token or any(char.isspace() for char in token):
        raise BootstrapError("github-credential")
    native = {key: value for key, value in os.environ.items()
              if key not in ("GH_TOKEN", "GITHUB_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN")}
    run(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--with-token"],
        stage="github-login", env=native, input=token + "\n")
    native.update(GH_TOKEN=token, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0")  # gitleaks:allow
    run(["gh", "auth", "setup-git", "--hostname", "github.com"], stage="github-git", env=native)
    run(["gh", "api", "user"], stage="github-auth", env=native)
    documents = Path.home() / "Documents"
    documents.mkdir(exist_ok=True)
    for repo in profile["repositories"]:
        run(["gh", "api", f"repos/{repo}"], stage="repository-permission", env=native)
        path, url = documents / repo.split("/")[1], f"https://github.com/{repo}.git"
        if not path.exists():
            run(["git", "clone", "--", url, str(path)], stage="repository-clone", env=native)
        origin = run(["git", "-C", str(path), "remote", "get-url", "origin"], stage="repository-origin", env=native)
        if origin.stdout.strip() != url:
            raise BootstrapError("repository-origin")
        run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], stage="repository-head", env=native)
        on_default_branch(path, native)
    return native


def on_default_branch(path, env):
    """Put a clone left on a detached commit back on its remote's default branch; a branch is left as it is.

    A spawned session pulls before working, which a detached clone cannot do. A checkout git refuses,
    such as one over uncommitted changes, keeps the clone where it is.
    """
    if run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"], stage="repository-branch", env=env, check=False).returncode == 0:
        return
    head = run(["git", "-C", str(path), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
               stage="repository-default", env=env, check=False).stdout.strip()
    branch = head.removeprefix("origin/") or "main"
    if run(["git", "-C", str(path), "checkout", "-q", branch], stage="repository-checkout", env=env, check=False).returncode == 0:
        run(["git", "-C", str(path), "pull", "-q", "--ff-only"], stage="repository-pull", env=env, check=False)


def _hub_settings(data):
    """Save the bridge's hub settings once the hub confirms the token names this node."""
    from one_conv.live import bridge, hubclient

    hub = data["hub"]
    try:
        client = hubclient.HubClient(hub["url"], hub["token"], hub["node"])
        if client.request("GET", "/v1/whoami").get("node") != hub["node"]:
            raise BootstrapError("hub-identity")
    except (hubclient.HubRefused, hubclient.HubUnavailable):
        raise BootstrapError("hub-identity") from None
    bridge.save_settings(hub["url"], hub["node"], 8789, hub["token"])
    write_private(bridge.bridge_dir() / "hub.json", {"url": hub["url"], "node": hub["node"], "token": hub["token"]})
    return bridge


def _launcher_link(checkout):
    """~/.local/bin/one-conv points at the checkout's launcher, and only there."""
    executable = Path.home() / ".local/bin/one-conv"
    executable.parent.mkdir(parents=True, exist_ok=True)
    if executable.exists() and not executable.is_symlink():
        raise BootstrapError("launcher-conflict")
    if executable.is_symlink() and executable.resolve() != (checkout / "bin/one-conv").resolve():
        raise BootstrapError("launcher-conflict")
    if not executable.exists() and not executable.is_symlink():
        executable.symlink_to(checkout / "bin/one-conv")
    return executable


def install_uv():
    """The uv that runs the one-conv launcher: the image's own, else Astral's pinned installer's."""
    existing = shutil.which("uv") or (Path.home() / ".local/bin/uv" if (Path.home() / ".local/bin/uv").exists() else None)
    if existing:
        return Path(existing)
    with tempfile.TemporaryDirectory() as folder:
        installer = Path(folder) / "install.sh"
        # Astral's CDN refuses urllib's user agent.
        run(["curl", "-LsSf", "-o", str(installer), f"https://astral.sh/uv/{UV_VERSION}/install.sh"], stage="uv-download")
        run(["sh", str(installer)], stage="uv-install", timeout=300,
            env={**os.environ, "UV_NO_MODIFY_PATH": "1", "UV_INSTALL_DIR": str(Path.home() / ".local/bin")})
    return Path.home() / ".local/bin/uv"


def install_native_claude():
    """A Claude Code native build in ~/.local/bin, first on the bridge's PATH: it updates itself, unlike the
    image's root-owned npm build, which Boat's platform pins and the box cannot upgrade."""
    native = Path.home() / ".local/bin/claude"
    if not native.exists():
        run(["claude", "install", "latest"], stage="claude-native", timeout=600)
    return native


def provision_spawner(data):
    """Make this box a spawner node from nothing: clones, git identity, uv, hub settings, the live
    install, spawner settings and the bridge's systemd unit. Every step is idempotent.

    The one-conv checkout follows main, not a pinned revision: a spawned session pulls it before work.
    """
    global STAGE
    env = prepare_repositories(data)
    checkout = Path.home() / "Documents/one-conv-cli"
    # -f restores every tracked file, such as a launcher symlink a snapshot restore brought back as an old file.
    run(["git", "-C", str(checkout), "checkout", "-q", "-f", "main"], stage="checkout-main", env=env)
    run(["git", "-C", str(checkout), "pull", "-q", "--ff-only"], stage="checkout-pull", env=env)
    revision = run(["git", "-C", str(checkout), "rev-parse", "HEAD"], stage="checkout-head", env=env).stdout.strip()
    git_user = data["git_user"]
    run(["git", "config", "--global", "user.name", git_user["name"]], stage="git-identity")
    run(["git", "config", "--global", "user.email", git_user["email"]], stage="git-identity")
    uv = install_uv()
    executable = _launcher_link(checkout)
    native_path = os.pathsep.join(dict.fromkeys([str(executable.parent), *os.environ.get("PATH", "").split(os.pathsep)]))
    os.environ["PATH"] = env["PATH"] = native_path
    sys.path.insert(0, str(checkout))
    STAGE = "hub-settings"
    _hub_settings(data)
    launcher = [str(uv), "run", "--script", str(checkout / "bin/one-conv")]
    spawner = data["spawner"]
    if spawner["harness"] == "claude":
        install_native_claude()
    run([*launcher, "live", "install", "--harness", spawner["harness"], "--yes"], stage="live-install", env=env, timeout=300)
    workspace = Path.home() / "Documents/one-conv-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    run([*launcher, "live", "spawner", "--harness", spawner["harness"], "--cwd", str(workspace),
         *[f"--arg={arg}" for arg in spawner["args"]], "--priority", str(spawner["priority"]),
         *(["--env-file", spawner["env_file"]] if spawner["env_file"] else []), "--yes"], stage="spawner-settings", env=env)
    user = os.environ.get("USER") or Path.home().name
    linger = run(["loginctl", "show-user", user, "-p", "Linger", "--value"], stage="linger", check=False)
    if linger.stdout.strip() != "yes":
        # Without lingering the user's systemd, and so the bridge, stops when no one is logged in.
        run(["sudo", "-n", "loginctl", "enable-linger", user], stage="linger")
    run([*launcher, "live", "serve", "--install", "--yes"], stage="service-install", env=env)
    from one_conv.live import spawn
    STAGE = "harness-auth"
    try:
        spawn.preflight(spawner["harness"], spawn.environment(spawner["env_file"]))
    except spawn.SpawnError:
        return {"status": "auth_needed", "stage": "harness-auth", "revision": revision}
    return {"status": "ready", "revision": revision}


def bootstrap(data):
    """Apply idempotent setup under a box-wide lock."""
    global STAGE
    STAGE = "payload"
    data = validate_payload(data)
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    root().chmod(0o700)
    with (root() / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return provision_spawner(data)


def main():
    """Emit whitelisted health only; exception text may contain provider secrets."""
    logging.basicConfig(level=logging.INFO)
    try:
        if len(sys.argv) != 2:
            raise BootstrapError("payload")
        result = bootstrap(read_private(Path(sys.argv[1])))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError, ImportError):
        result = {"status": "unconfirmed" if STAGE == "service-install" else "failed", "stage": STAGE}
        log.error("spawner bootstrap failed at operation=%s", STAGE)
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
