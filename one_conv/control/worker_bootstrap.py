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
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
OP_VERSION = "2.32.0"
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
    skills = destination / ".claude/skills"
    for name in data["profile"]["skills"]:
        target = Path.home() / "Documents/claudine/.claude/skills" / name
        if not target.is_dir():
            raise BootstrapError("skill-unavailable")
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
        return ["claude", *options, "--print", "--output-format", "json", PROBE]
    if harness == "codex":
        return ["codex", "exec", *options, "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--json", PROBE]
    return ["omp", *([f"--model={model}"] if model else []), "--print", "--mode=json", PROBE]


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


def _install_helper():
    target = Path.home() / ".local/lib/one-conv/worker_bootstrap.py"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if Path(__file__).resolve() != target.resolve():
        shutil.copyfile(__file__, target)
        target.chmod(0o600)
    return target


def _bridge(data, launcher):
    """One pool-wide bridge holds its existing singleton lock and never embeds a token in argv."""
    from one_conv.live import bridge

    hub = data["hub"]
    bridge.save_settings(hub["url"], hub["node"], 8789, hub["token"])
    if not bridge.status().get("running"):
        log_path = root() / "bridge.log"
        with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as stream:
            subprocess.Popen([*launcher, "live", "serve"], stdout=stream, stderr=stream,
                             stdin=subprocess.DEVNULL, start_new_session=True)


def _collector(data, address, launcher):
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
    mapping["owners"][address] = {"cost_center_id": data["spec"]["name"],
                                  "worker_id": f"{data['hub']['node']}/{address}", "billing_scope": scope}
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
        write_private(run_path, {"harness": data["spec"]["harness"], "model": model, "prompt": prompt})
        return [sys.executable, str(helper), "run", str(run_path)]

    harness = data["spec"]["harness"]
    original = spawn.COMMANDS[harness]
    record.update(phase="launching", cwd=str(cwd))
    write_private(path, record)
    spawn.COMMANDS[harness] = command
    try:
        try:
            spawned = spawn.start(harness, data["spec"]["prompt"], cwd, data["spec"]["model"], trust_folder=True)
        except spawn.SpawnError:
            raise BootstrapError("native-launch") from None
    finally:
        spawn.COMMANDS[harness] = original
    record.update(phase="registered", address=spawned.address, tmux=spawned.tmux)
    write_private(path, record)
    try:
        team.set_name(data["spec"]["name"], spawned.address, lambda candidate: any(s.address == candidate for s in router.list_all()))
    except team.TeamError:
        raise BootstrapError("native-name") from None
    return {"status": "registered", "address": spawned.address}


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
        sys.path.insert(0, str(checkout))
        cwd = worktree(data, env)
        harness = data["spec"]["harness"]
        if shutil.which(harness) is None:
            return {"status": "failed", "stage": "harness-unavailable"}
        if not data["profile"]["probe_enabled"]:
            return {"status": "unverified", "stage": "model-probe-disabled", "model_verified": False}
        if data.get("restart"):
            record.pop("model_verified", None)
        if not record.get("model_verified") or time.time() - record.get("probe_verified_at", 0) > 300:
            result = run(probe_command(harness, data["spec"]["model"]), stage="model-probe", env=env,
                         cwd=cwd, timeout=120, check=False)
            if result.returncode or not probe_answer(harness, result.stdout):
                return {"status": "auth_needed", "stage": "model-probe", "authenticated": False}
            record.update(model_verified=True, probe_verified_at=time.time())
            write_private(path, record)
        launcher = [shutil.which("uv") or str(Path.home() / ".local/bin/uv"), "run", "--script", str(checkout / "bin/one-conv")]
        run([*launcher, "live", "install", "--harness", harness, "--yes"], stage="live-install", env=env)
        STAGE = "native-launch"
        native = _start(data, env, cwd, path, record)
        if native["status"] != "registered":
            return native
        _bridge(data, launcher)
        STAGE = "hub-readiness"
        health = _readiness(data, native["address"])
        if health["status"] == "ready":
            if not _collector(data, native["address"], launcher):
                return {"status": "unconfirmed", "address": native["address"], "stage": "usage-collector"}
            health["collector"] = True
        return health


def run_native(path):
    """Replace the helper with the native executable; prompts remain a direct argv element."""
    data = read_private(path)
    credentials = json.loads((root() / "credentials.json").read_text())
    env = {**os.environ, **credentials}
    harness, model = data["harness"], data["model"]
    if harness not in ("claude", "codex", "omp"):
        raise BootstrapError("native-harness")
    args = [harness, *([f"--model={model}"] if harness == "omp" and model else
                      ["--model", model] if model else []), data["prompt"]]
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
        result = {"status": "unconfirmed" if STAGE in ("native-launch", "hub-readiness", "usage-sync", "usage-status") else "failed", "stage": STAGE}
        log.error("worker bootstrap failed at operation=%s", STAGE)
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
