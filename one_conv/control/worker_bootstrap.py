#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Bootstrap selected cloud repositories and native agents from a private JSON payload.

Boat runs this dependency-free helper with fixed arguments. Secrets and prompts
remain in private files or descendant environments, never in a shell command.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
OP_VERSION = "2.32.0"
OMP_VERSION = "18.0.6"
# Official v18.0.6 release assets and SHA256SUMS.txt pin both content and download bounds.
OMP_RELEASES = {
    "x86_64": ("x64", 183637192, "9cbc78be9ba6357b53a6a055c98ad7afee4c00d20a320d51bb76a949b8a5a444", 62),
    "aarch64": ("arm64", 147113976, "8566b0a8cbc6fb4a01decc937fa37572bded86c3de75f811ef37fb2fab43f912", 183),
}
GITHUB_REF = "op://Claudine/GitHub gh token/credential"
PROBE = "Reply exactly ONE_CONV_READY. Do not use tools."
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


def validate_payload(data):
    """Validate selectors before any credential read, clone, path lookup or native launch."""
    if (not isinstance(data, dict) or data.get("operation") not in ("start", "stop")
            or not isinstance(data.get("job_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", data["job_id"])):
        raise BootstrapError("payload")
    if data["operation"] == "stop":
        return data
    spec, profile, hub = data.get("spec"), data.get("profile"), data.get("hub")
    if (not isinstance(spec, dict) or not isinstance(profile, dict) or not isinstance(hub, dict)
            or spec.get("harness") not in ("claude", "codex", "omp")
            or not isinstance(spec.get("prompt"), str) or not spec["prompt"].strip() or "\x00" in spec["prompt"]):
        raise BootstrapError("payload")
    for name in (spec.get("name"), spec.get("topic"), hub.get("node"), *profile.get("skills", [])):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name):
            raise BootstrapError("payload")
    repos = profile.get("repositories")
    if (not isinstance(repos, list) or not repos or not all(isinstance(repo, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repo)
            and repo.split("/")[1] not in (".", "..") and not repo.endswith(".git") for repo in repos)
            or len({repo.split("/")[1].lower() for repo in repos}) != len(repos)
            or profile.get("work_repository") not in repos or "ClementWalter/one-conv-cli" not in repos):
        raise BootstrapError("payload")
    refs = profile.get("credential_refs")
    if (not isinstance(refs, dict) or any(not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}(?:_API_KEY|_AUTH_TOKEN|_ACCESS_TOKEN|_OAUTH_TOKEN)", key)
            or not isinstance(ref, str) or not ref.startswith("op://") or "\x00" in ref for key, ref in refs.items())):
        raise BootstrapError("payload")
    revisions = profile.get("revisions", {})
    if not isinstance(revisions, dict) or any(repo not in repos or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision) for repo, revision in revisions.items()):
        raise BootstrapError("payload")
    args = profile.get("harness_args")
    if (not isinstance(args, dict) or set(args) != {"claude", "codex", "omp"}
            or any(not isinstance(values, list) or len(values) > 32 or any(not isinstance(arg, str) or len(arg) > 1024
                   or "\x00" in arg or "\n" in arg for arg in values) for values in args.values())):
        raise BootstrapError("payload")
    model = spec.get("model")
    if model is not None and (not isinstance(model, str) or not model or model.startswith("-") or "\x00" in model):
        raise BootstrapError("payload")
    url = urlsplit(hub.get("url", ""))
    if url.username or url.password or not url.hostname or url.scheme not in ("https", "http"):
        raise BootstrapError("payload")
    for token in (data.get("service_account"), hub.get("token")):
        if not isinstance(token, str) or not token or any(char.isspace() for char in token) or "\x00" in token:
            raise BootstrapError("payload")
    return data


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


def install_omp():
    """Verify a bounded official ELF download before atomically exposing the executable."""
    global STAGE
    STAGE = "omp-install"
    release = OMP_RELEASES.get(platform.machine()) if platform.system() == "Linux" else None
    if release is None:
        raise BootstrapError("omp-architecture")
    arch, size, checksum, machine = release
    url = f"https://github.com/can1357/oh-my-pi/releases/download/v{OMP_VERSION}/omp-linux-{arch}"
    target = Path.home() / ".local/bin/omp"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".omp-", dir=target.parent)
    staged = Path(temporary)
    digest, count, header = hashlib.sha256(), 0, b""
    deadline = time.monotonic() + 180
    try:
        with os.fdopen(descriptor, "wb") as stream:
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    if urlsplit(response.geturl()).scheme != "https":
                        raise BootstrapError("omp-download")
                    while chunk := response.read(1024 * 1024):
                        count += len(chunk)
                        if count > size or time.monotonic() > deadline:
                            raise BootstrapError("omp-download")
                        if not header:
                            header = chunk[:64]
                        digest.update(chunk)
                        stream.write(chunk)
            except (OSError, ValueError):
                raise BootstrapError("omp-download") from None
            stream.flush()
            os.fsync(stream.fileno())
        if count != size or digest.hexdigest() != checksum:
            raise BootstrapError("omp-checksum")
        if len(header) < 64 or header[:7] != b"\x7fELF\x02\x01\x01" or int.from_bytes(header[18:20], "little") != machine:
            raise BootstrapError("omp-architecture")
        staged.chmod(0o700)
        with tempfile.TemporaryDirectory(prefix="omp-verify-") as isolated:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": isolated, "XDG_CONFIG_HOME": isolated,
                   "PI_CODING_AGENT_DIR": isolated}
            version = run([str(staged), "--version"], stage="omp-version", env=env, timeout=30)
            if version.stdout.strip() != f"omp/{OMP_VERSION}":
                raise BootstrapError("omp-version")
            help_text = run([str(staged), "--help"], stage="omp-help", env=env, timeout=30).stdout
            if not all(flag in help_text for flag in ("--no-tools", "--mode", "--approval-mode")):
                raise BootstrapError("omp-help")
        staged.chmod(0o755)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
    return target


def omp_credential_count(path, *, active_anthropic=False):
    """Read numeric native auth presence without exposing credentials or changing native state."""
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as database:
            if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_credentials'").fetchone():
                return 0
            query = "SELECT count(*) FROM auth_credentials"
            if active_anthropic:
                query += " WHERE provider='anthropic' AND credential_type='oauth' AND disabled_cause IS NULL"
            return database.execute(query).fetchone()[0]
    except sqlite3.Error:
        return None


def prepare_omp_auth(data, env):
    """Import the selected Boat Pi identity once through OMP's official local importer."""
    if data["spec"]["harness"] != "omp" or data["profile"].get("boat_agent_credentials") is not True:
        return True
    home = Path.home()
    agent = home / ".omp/agent"
    database = agent / "agent.db"
    args = data["profile"]["harness_args"]["omp"]
    # Claude Code's OAuth variable is not an OMP provider credential.
    provider_env = any(value and key != "CLAUDE_CODE_OAUTH_TOKEN" and
                       (key.endswith(("_API_KEY", "_OAUTH_TOKEN", "_ACCESS_TOKEN", "_AUTH_TOKEN"))
                        or key in ("COPILOT_GITHUB_TOKEN", "HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "GITLAB_TOKEN",
                                   "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK",
                                   "AWS_WEB_IDENTITY_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS"))
                       for key, value in env.items())
    # Existing identities, blocks, profiles, brokers and explicit credentials retain their own resolution.
    if (omp_credential_count(database) != 0 or (agent / "auth.json").exists()
            or data["profile"]["credential_refs"]
            or provider_env
            or any(env.get(key) for key in ("OMP_PROFILE", "PI_PROFILE", "PI_CONFIG_DIR", "PI_CODING_AGENT_DIR", "OMP_AUTH_BROKER_URL"))
            or any(arg in ("--profile", "--config", "--api-key") or arg.startswith(("--profile=", "--config=", "--api-key=")) for arg in args)
            or any((directory / name).exists() for directory in (home, home / ".omp", agent)
                   for name in (".env", "config.yml", "config.yaml"))
            or any(env.get(key) and (Path(env[key]) / "omp").exists() for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"))):
        return True
    try:
        descriptor = os.open(home / ".pi/agent/auth.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600
                    or metadata.st_uid != os.getuid() or metadata.st_size > 128_000):
                return False
            raw = stream.read(128_001)
            if len(raw) > 128_000:
                return False
            source = json.loads(raw)
        credential = source.get("anthropic") if isinstance(source, dict) else None
        if (not isinstance(credential, dict) or credential.get("type") != "oauth"
                or any(not isinstance(credential.get(key), str) or not credential[key]
                       or any(character.isspace() or character == "\x00" for character in credential[key])
                       for key in ("access", "refresh"))
                or type(credential.get("expires")) is not int or credential["expires"] <= 0):
            return False
        converted = {"type": "claude", "access_token": credential["access"], "refresh_token": credential["refresh"],
                     "expired": datetime.fromtimestamp(credential["expires"] / 1000, tz=UTC).isoformat()}
        for original, target in (("email", "email"), ("accountId", "account_id")):
            if original in credential:
                if not isinstance(credential[original], str) or not credential[original] or "\x00" in credential[original]:
                    return False
                converted[target] = credential[original]
        root().mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="omp-auth-", dir=root()) as directory:
            path = Path(directory) / "anthropic.json"
            write_private(path, converted)
            result = run(["omp", "auth-broker", "import", str(path), "--provider=anthropic", "--json"],
                         stage="omp-auth-import", env=env, cwd=root(), timeout=30)
            receipt = json.loads(result.stdout)
            imported = receipt.get("imported") if isinstance(receipt, dict) else None
            if (not isinstance(imported, list) or len(imported) != 1 or not isinstance(imported[0], dict)
                    or imported[0].get("provider") != "anthropic" or receipt.get("skipped")):
                return False
        return omp_credential_count(database, active_anthropic=True) == 1
    except (OSError, ValueError, OverflowError, TypeError, BootstrapError):
        return False


def prepare_repositories(data):
    """Read the narrow GitHub credential and verify each explicitly selected clone."""
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
    native.update(GH_TOKEN=token, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0")
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
        revision = profile.get("revisions", {}).get(repo)
        if revision:
            run(["git", "-C", str(path), "fetch", "origin", revision], stage="repository-fetch", env=native)
            run(["git", "-C", str(path), "checkout", "--detach", revision], stage="repository-revision", env=native)
    # Only explicitly requested provider credentials enter the native harness environment.
    for key, ref in profile["credential_refs"].items():
        value = run([str(op), "read", ref], stage="provider-vault", env=env).stdout.strip()
        if not value or "\x00" in value:
            raise BootstrapError("provider-credential")
        native[key] = value
    write_private(root() / "credentials.json", {key: native[key] for key in ("GH_TOKEN", *profile["credential_refs"])})
    return native


def worktree(data, env):
    """Each native worker edits its own branch and working tree."""
    source = Path.home() / "Documents" / data["profile"]["work_repository"].split("/")[1]
    destination = Path.home() / "Documents/workers" / data["job_id"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        run(["git", "-C", str(source), "worktree", "add", "-b", f"workers/{data['job_id']}", str(destination), "HEAD"],
            stage="worktree-create", env=env)
    run(["git", "-C", str(destination), "rev-parse", "--verify", "HEAD"], stage="worktree-check", env=env)
    for name in data["profile"]["skills"]:
        checkout = Path.home() / "Documents/claudine"
        target = checkout / ".claude/skills" / name
        if not target.is_dir():
            try:
                relative = target.resolve().relative_to(checkout.resolve())
            except ValueError:
                raise BootstrapError("skill-unavailable") from None
            index = run(["git", "-C", str(checkout), "ls-files", "--stage", "-z"], stage="skill-submodule-index", env=env)
            for entry in index.stdout.split("\x00"):
                metadata, separator, tracked = entry.partition("\t")
                if (separator and metadata.startswith("160000 ")
                        and (Path(tracked) == relative or Path(tracked) in relative.parents)):
                    run(["git", "-C", str(checkout), "submodule", "update", "--init", "--depth", "1", "--", tracked],
                        stage="skill-submodule", env=env)
        if not target.is_dir():
            raise BootstrapError("skill-unavailable")
        for skills in (destination / ".claude/skills", Path.home() / ".codex/skills", Path.home() / ".omp/agent/skills"):
            skills.mkdir(parents=True, exist_ok=True)
            link = skills / name
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            if link.exists() or link.is_symlink():
                raise BootstrapError("skill-conflict")
            link.symlink_to(target, target_is_directory=True)
    return destination


def probe_command(harness, model):
    """Use a tiny native answer to verify provider access and the selected model."""
    options = ["--model", model] if model else []
    if harness == "claude":
        return ["claude", *options, "--print", "--output-format", "json", "--tools", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--", PROBE]
    if harness == "codex":
        return ["codex", "exec", *options, "--sandbox", "read-only", "--skip-git-repo-check", "--json",
                "--ignore-user-config", "--ignore-rules", "--disable", "shell_tool", "--disable", "multi_agent",
                "--disable", "apps", "--disable", "plugins", "-c", 'web_search="disabled"', PROBE]
    return ["omp", *([f"--model={model}"] if model else []), "--print", "--mode=json", "--no-tools",
            "--no-extensions", "--no-skills", "--no-rules", PROBE]


def probe_answer(harness, output):
    """Accept a completed assistant answer, never an echoed prompt or a tool result."""
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if (harness == "claude" and row.get("type") == "result" and row.get("is_error") is False
                and row.get("result", "").strip() == "ONE_CONV_READY"):
            return True
        if harness == "codex" and row.get("type") == "item.completed":
            item = row.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text", "").strip() == "ONE_CONV_READY":
                return True
        if harness == "omp" and row.get("type") == "message_end":
            message = row.get("message") or {}
            text = "".join(block.get("text", "") for block in message.get("content", []) if block.get("type") == "text")
            if message.get("role") == "assistant" and message.get("stopReason") not in ("error", "aborted") and text.strip() == "ONE_CONV_READY":
                return True
    return False


def probe_session(harness, output):
    """Read only native session identifiers from the probe's structured stream."""
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ident = row.get("session_id") if harness == "claude" else (
            row.get("thread_id") if harness == "codex" and row.get("type") == "thread.created" else
            row.get("id") if harness == "omp" and row.get("type") == "session" else None)
        if isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", ident):
            return f"{harness}:{ident}"
    return None


def _install_helper():
    target = Path.home() / ".local/lib/one-conv/worker_bootstrap.py"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if Path(__file__).resolve() != target.resolve():
        shutil.copyfile(__file__, target)
        target.chmod(0o600)
    return target


def _bridge(data, launcher):
    """One pool-wide bridge holds its existing singleton lock and never embeds a token in argv."""
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
    if not bridge.status().get("running"):
        log_path = root() / "bridge.log"
        with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as stream:
            subprocess.Popen([*launcher, "live", "serve"], stdout=stream, stderr=stream,
                             stdin=subprocess.DEVNULL, start_new_session=True)


def _collector(data, address, launcher, probe_addresses=()):
    """Keep one pool-wide numeric usage collector with an atomically refreshed owner map."""
    mapping_path = root() / "usage-map.json"
    mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {"schema_version": 1, "histories": [], "owners": {}}
    scope = data["profile"].get("billing_scope") or f"unknown-pool-{data['pool_id']}"
    harness = data["spec"]["harness"]
    histories = {"claude": Path.home() / ".claude/projects", "codex": Path.home() / ".codex/sessions",
                 "omp": Path.home() / ".omp/agent/sessions"}
    entry = {"source": harness, "path": str(histories[harness]), "billing_scope": scope}
    if entry not in mapping["histories"]:
        mapping["histories"].append(entry)
    for owner in (address, *probe_addresses):
        mapping["owners"][owner] = {"cost_center_id": data["spec"]["name"],
                                    "worker_id": f"{data['hub']['node']}/{owner}", "billing_scope": scope}
    write_private(mapping_path, mapping)
    hub = data["hub"]
    env = {**os.environ, "ONE_CONV_HUB_URL": hub["url"], "ONE_CONV_HUB_NODE": hub["node"],
           "ONE_CONV_HUB_TOKEN": hub["token"], "PYTHONOPTIMIZE": "2"}
    run([*launcher, "hr", "sync", "--mapping", str(mapping_path), "--hub", "--json"], stage="usage-sync", env=env)
    state_path = root() / "collector.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    pid = state.get("pid")
    if type(pid) is int and pid > 0:
        process = run(["ps", "-p", str(pid), "-o", "args="], stage="usage-status", check=False)
        if process.returncode == 0 and "hr watch" in process.stdout and str(mapping_path) in process.stdout:
            return True
    log_path = root() / "collector.log"
    with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as stream:
        process = subprocess.Popen([*launcher, "hr", "watch", "--mapping", str(mapping_path), "--hub", "--interval", "5",
                                    "--max-backoff", "60", "--json"], env=env, stdout=stream, stderr=stream,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    write_private(state_path, {"pid": process.pid})
    time.sleep(0.1)
    return process.poll() is None


def _start(data, env, cwd, path, record):
    """A durable launch marker blocks duplicate tmux agents after a lost acknowledgement."""
    from one_conv.live import router, spawn, team

    if record.get("phase") == "launching":
        return {"status": "unconfirmed", "stage": "native-launch"}
    address = record.get("address")
    if address:
        if not any(session.address == address for session in router.list_all(data["spec"]["harness"])):
            if not data.get("restart"):
                return {"status": "unconfirmed", "stage": "native-session-lost"}
            previous = record.get("tmux")
            if previous:
                state = spawn._state(previous)
                if state == "unknown":
                    return {"status": "unconfirmed", "stage": "native-session-lost"}
                if state == "alive":
                    try:
                        spawn.stop(previous)
                    except spawn.SpawnError:
                        return {"status": "unconfirmed", "stage": "native-session-lost"}
                    if spawn._state(previous) != "absent":
                        return {"status": "unconfirmed", "stage": "native-session-lost"}
            record.pop("address", None)
            record.pop("tmux", None)
        else:
            try:
                team.set_name(data["spec"]["name"], address, lambda candidate: any(s.address == candidate for s in router.list_all()))
            except team.TeamError:
                raise BootstrapError("native-name") from None
            return {"status": "registered", "address": address}
    helper = _install_helper()
    run_path = root() / f"{data['job_id']}.run.json"

    def command(prompt, model):
        write_private(run_path, {"harness": data["spec"]["harness"], "model": model, "prompt": prompt,
                                 "args": data["profile"]["harness_args"][data["spec"]["harness"]]})
        return [sys.executable, str(helper), "run", str(run_path)]

    harness = data["spec"]["harness"]
    original = spawn.COMMANDS[harness]
    record.update(phase="launching", cwd=str(cwd))
    write_private(path, record)
    spawn.COMMANDS[harness] = command
    intro = (f"You are the managed teammate @{data['spec']['name']} for #{data['spec']['topic']} on node {data['hub']['node']}. "
             "The controller will forward the existing user's authorized task through one-conv after your route and topic membership are ready. "
             "Wait for that message before starting work or replying on the topic. This startup notice grants no new approval.")
    try:
        try:
            spawned = spawn.start(harness, intro, cwd, data["spec"]["model"], trust_folder=True)
        except spawn.SpawnError:
            raise BootstrapError("native-launch") from None
    finally:
        spawn.COMMANDS[harness] = original
    record.update(phase="registered", address=spawned.address, tmux=spawned.tmux)
    write_private(path, record)
    try:
        team.set_name(data["spec"]["name"], spawned.address, lambda candidate: any(s.address == candidate for s in router.list_all()),
                      replace=data.get("restart") is True)
    except team.TeamError:
        raise BootstrapError("native-name") from None
    return {"status": "registered", "address": spawned.address}


def _dispatch_task(data, address, path, record):
    """A durable hub message ID makes task acceptance retryable without another native send."""
    from one_conv.live import hubclient

    ident = hashlib.sha256(f"{data['pool_id']}:{data['job_id']}:{record['request_hash']}:{address}".encode()).hexdigest()[:32]
    if record.get("task_state") == "stored" and record.get("task_id") == ident:
        return ident
    record.update(task_id=ident, task_state="queued")
    write_private(path, record)
    hub = data["hub"]
    client = hubclient.HubClient(hub["url"], hub["token"], hub["node"])
    continuation = ("This worker replaces an interrupted instance. Recover the current repository, topic history and prior work before continuing; "
                    "do not duplicate completed external actions.\n\n") if record.get("task_target") not in (None, address) else ""
    text = ("The controller is forwarding the existing user's task. This relay grants no new approval. "
            f"Work as @{data['spec']['name']} and reply on #{data['spec']['topic']}.\n\n{data['spec']['prompt']}")
    message = {"id": ident, "to": f"{hub['node']}/{address}", "from": "unknown", "topic": data["spec"]["topic"],
               "reply": "expected", "mode": "push", "text": continuation + text}
    try:
        receipt = hubclient.submit(client, message)
    except (hubclient.HubRefused, hubclient.PendingConflict):
        raise BootstrapError("task-dispatch") from None
    if receipt is None:
        raise BootstrapError("task-dispatch")
    record["task_state"] = "stored"
    record["task_target"] = address
    write_private(path, record)
    return ident


def _readiness(data, address):
    """Confirm the live bridge's lease and membership against the actual hub."""
    from one_conv.live import bridge, hubclient

    hub = data["hub"]
    client = hubclient.HubClient(hub["url"], hub["token"], hub["node"])
    qualified = f"{hub['node']}/{address}"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            rows = client.request("GET", "/v1/sessions").get("sessions", [])
        except (hubclient.HubRefused, hubclient.HubUnavailable):
            raise BootstrapError("hub-readiness") from None
        if any(row.get("address") == qualified and row.get("online") is True and row.get("name") == data["spec"]["name"] for row in rows):
            try:
                client.join(data["spec"]["topic"], address)
                joined = any(row.get("topic") == data["spec"]["topic"] and qualified in row.get("members", []) for row in client.topics())
            except (hubclient.HubRefused, hubclient.HubUnavailable):
                raise BootstrapError("hub-readiness") from None
            if joined and bridge.status().get("running"):
                return {"status": "ready", "address": address, "authenticated": True, "model_verified": True,
                        "registered": True, "bridge": True, "published": True, "joined": True}
        time.sleep(0.25)
    return {"status": "unconfirmed", "address": address, "stage": "hub-readiness"}


def stop_worker(data, path, record):
    """Stop just the tracked native process, retaining the pool and worktree."""
    from one_conv.live import spawn

    if not record or record.get("phase") == "stopped":
        return {"status": "stopped"}
    target = record.get("tmux")
    if not target and record.get("cwd"):
        rows = [row for row in spawn.listing() if row.get("cwd") == record["cwd"]]
        if len(rows) == 1:
            target = rows[0]["tmux"]
        elif rows:
            return {"status": "unconfirmed", "stage": "worker-stop"}
    if target:
        if spawn._state(target) != "absent":
            try:
                spawn.stop(target)
            except spawn.SpawnError:
                return {"status": "unconfirmed", "stage": "worker-stop"}
    elif record.get("phase") == "launching":
        return {"status": "unconfirmed", "stage": "worker-stop"}
    record.update(phase="stopped")
    write_private(path, record)
    return {"status": "stopped"}


def bootstrap(data):
    """Apply idempotent setup under a pool-wide lock and retain native launch outcomes."""
    global STAGE
    STAGE = "payload"
    data = validate_payload(data)
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    root().chmod(0o700)
    with (root() / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root() / f"{data['job_id']}.json"
        record = json.loads(path.read_text()) if path.exists() else {}
        if data["operation"] == "stop":
            checkout = Path.home() / "Documents/one-conv-cli"
            sys.path.insert(0, str(checkout))
            return stop_worker(data, path, record)
        request_hash = hashlib.sha256(json.dumps(data["spec"], sort_keys=True).encode()).hexdigest()
        if record.get("request_hash") not in (None, request_hash):
            raise BootstrapError("request-conflict")
        record["request_hash"] = request_hash
        write_private(path, record)
        env = prepare_repositories(data)
        checkout = Path.home() / "Documents/one-conv-cli"
        executable = Path.home() / ".local/bin/one-conv"
        executable.parent.mkdir(parents=True, exist_ok=True)
        if executable.exists() and not executable.is_symlink():
            raise BootstrapError("launcher-conflict")
        if executable.is_symlink() and executable.resolve() != (checkout / "bin/one-conv").resolve():
            raise BootstrapError("launcher-conflict")
        if not executable.exists() and not executable.is_symlink():
            executable.symlink_to(checkout / "bin/one-conv")
        native_path = str(executable.parent) + os.pathsep + os.environ.get("PATH", "")
        os.environ["PATH"] = native_path
        env["PATH"] = native_path
        sys.path.insert(0, str(checkout))
        cwd = worktree(data, env)
        harness = data["spec"]["harness"]
        if harness == "omp" and shutil.which(harness) is None:
            install_omp()
        if shutil.which(harness) is None:
            return {"status": "failed", "stage": "harness-unavailable"}
        if not prepare_omp_auth(data, env):
            return {"status": "auth_needed", "stage": "omp-auth-import", "authenticated": False}
        if not data["profile"]["probe_enabled"]:
            return {"status": "unverified", "stage": "model-probe-disabled", "model_verified": False}
        if data.get("restart"):
            record.pop("model_verified", None)
        if not record.get("model_verified") or time.time() - record.get("probe_verified_at", 0) > 300:
            result = run(probe_command(harness, data["spec"]["model"]), stage="model-probe", env=env,
                         cwd=cwd, timeout=120, check=False)
            probe = probe_session(harness, result.stdout)
            if probe:
                record["probe_addresses"] = list(dict.fromkeys([*record.get("probe_addresses", []), probe]))
                write_private(path, record)
            if result.returncode or not probe_answer(harness, result.stdout):
                return {"status": "auth_needed", "stage": "model-probe", "authenticated": False}
            record.update(model_verified=True, probe_verified_at=time.time())
            write_private(path, record)
        launcher = [shutil.which("uv") or str(Path.home() / ".local/bin/uv"), "run", "--script", str(checkout / "bin/one-conv")]
        run([*launcher, "live", "install", "--harness", harness, "--yes"], stage="live-install", env=env)
        STAGE = "hub-startup"
        _bridge(data, launcher)
        STAGE = "native-launch"
        native = _start(data, env, cwd, path, record)
        if native["status"] != "registered":
            return native
        STAGE = "hub-readiness"
        health = _readiness(data, native["address"])
        if health["status"] == "ready":
            if not _collector(data, native["address"], launcher, record.get("probe_addresses", [])):
                return {"status": "unconfirmed", "address": native["address"], "stage": "usage-collector"}
            health["collector"] = True
            STAGE = "task-dispatch"
            health["task_id"] = _dispatch_task(data, native["address"], path, record)
        return health


def run_native(path):
    """Replace the helper with the native executable; prompts remain a direct argv element."""
    data = read_private(path)
    credentials = json.loads((root() / "credentials.json").read_text())
    env = {**os.environ, **credentials}
    harness, model = data["harness"], data["model"]
    if harness not in ("claude", "codex", "omp"):
        raise BootstrapError("native-harness")
    args = [harness, *data.get("args", []), *([f"--model={model}"] if harness == "omp" and model else
                      ["--model", model] if model else []), "--", data["prompt"]]
    os.execvpe(harness, args, env)


def main():
    """Emit whitelisted health only; exception text may contain provider secrets."""
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        run_native(Path(sys.argv[2]))
        return
    try:
        if len(sys.argv) != 2:
            raise BootstrapError("payload")
        result = bootstrap(read_private(Path(sys.argv[1])))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError, ImportError):
        result = {"status": "unconfirmed" if STAGE in ("native-launch", "hub-readiness", "usage-sync", "usage-status", "task-dispatch") else "failed", "stage": STAGE}
        log.error("worker bootstrap failed at operation=%s", STAGE)
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
