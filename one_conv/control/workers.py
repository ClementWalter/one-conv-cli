"""Durable, serial provisioning of native agents on shared Boat worker pools.

An ambiguous provider acknowledgement reserves its pool until reconciliation;
retrying a request never authorizes a second sandbox or a second agent.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from one_conv.live.hubclient import HubClient, HubRefused, HubUnavailable, check_url

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
BOX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
ACTIVE = {"queued", "provisioning", "ready", "auth_needed", "unverified", "unconfirmed"}
DEFAULT_PROFILE = {
    "repositories": ["ClementWalter/claudine", "ClementWalter/one-conv-cli"],
    "work_repository": "ClementWalter/one-conv-cli", "skills": [],
    "type": "default", "slots": 4, "credential_environment": "claudine",
    "environment": "base", "probe_enabled": True, "credential_refs": {},
    "boat_agent_credentials": False,
    "billing_scope": None, "revisions": {},
    "harness_args": {"claude": ["--dangerously-skip-permissions", "--settings", '{"skipDangerousModePermissionPrompt":true}'],
                     "codex": ["--dangerously-bypass-approvals-and-sandbox"], "omp": ["--approval-mode=yolo"]},
}


class WorkerError(RuntimeError):
    """A safe operation label without provider output or credential values."""


class Unconfirmed(WorkerError):
    """A side effect may have happened and must not be repeated blindly."""


class PreflightRefused(WorkerError):
    """A verified unsafe configuration prevented sandbox allocation."""


def machine_status(state):
    """Normalize Boat's documented API states and stopped CLI label."""
    if state in ("ready", "idle", "running"):
        return "available"
    if state in ("archived", "stopped"):
        return "stopped"
    return None


def private_json(path: Path, value: dict) -> None:
    """Atomically replace private state and flush it before an external effect."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {"jobs": {}, "pools": {}}
    except (OSError, ValueError):
        raise WorkerError("worker state is unreadable; repair it before provisioning") from None
    if not isinstance(value, dict) or not all(isinstance(value.get(key), dict) for key in ("jobs", "pools")):
        raise WorkerError("worker state is invalid; repair it before provisioning")
    return value


def _repository(value: str) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value)
            is not None and value.split("/")[1] not in (".", "..") and not value.endswith(".git"))


def _profile(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_PROFILE):
        raise ValueError("unsupported worker profile fields")
    profile = {**DEFAULT_PROFILE, **value}
    repos = profile["repositories"]
    if (not isinstance(repos, list) or not repos or not all(_repository(repo) for repo in repos)
            or len({repo.split("/")[1].lower() for repo in repos}) != len(repos)
            or profile["work_repository"] not in repos or "ClementWalter/one-conv-cli" not in repos):
        raise ValueError("profile repositories must be distinct GitHub repositories including one-conv-cli")
    if type(profile["slots"]) is not int or not 1 <= profile["slots"] <= 10:
        raise ValueError("resident slots must be between 1 and 10; active capacity is unverified")
    if profile["type"] not in ("small", "default", "large"):
        raise ValueError("unsupported worker machine type")
    if type(profile["probe_enabled"]) is not bool:
        raise ValueError("probe_enabled must be a boolean")
    if type(profile["boat_agent_credentials"]) is not bool:
        raise ValueError("boat_agent_credentials must be a boolean")
    if not isinstance(profile["skills"], list) or not all(isinstance(s, str) and NAME.fullmatch(s) for s in profile["skills"]):
        raise ValueError("skills must be explicit names in the claudine skill directory")
    for key in ("environment", "credential_environment"):
        if not isinstance(profile[key], str) or not NAME.fullmatch(profile[key]):
            raise ValueError("environment must be a named Boat environment")
    refs = profile["credential_refs"]
    if (not isinstance(refs, dict) or any(not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}(?:_API_KEY|_AUTH_TOKEN|_ACCESS_TOKEN|_OAUTH_TOKEN)", key)
            or not isinstance(ref, str) or not ref.startswith("op://") or "\x00" in ref for key, ref in refs.items())):
        raise ValueError("credential_refs must map environment names to vault references")
    if profile["billing_scope"] is not None and (not isinstance(profile["billing_scope"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", profile["billing_scope"])):
        raise ValueError("billing_scope must be a stable account label or null")
    revisions = profile["revisions"]
    if (not isinstance(revisions, dict) or any(repo not in repos or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision) for repo, revision in revisions.items())):
        raise ValueError("revisions must pin selected repositories to full commit hashes")
    args = profile["harness_args"]
    if (not isinstance(args, dict) or set(args) != {"claude", "codex", "omp"}
            or any(not isinstance(values, list) or len(values) > 32 or any(not isinstance(arg, str) or len(arg) > 1024
                   or "\x00" in arg or "\n" in arg for arg in values) for values in args.values())):
        raise ValueError("harness_args must contain direct argument lists for claude, codex and omp")
    return copy.deepcopy(profile)


class BoatCLI:
    """Capture every provider response; transfer user text and secrets only in private JSON."""

    def __init__(self, executable="box", service_account_file=None):
        self.executable = executable
        self.service_account_file = Path(service_account_file) if service_account_file else None

    @staticmethod
    def _receipt(output, operation):
        """Accept complete JSON or creation events ending in one consistent ready receipt."""
        if len(output) > 2_000_000 or len(output.encode()) > 2_000_000:
            raise Unconfirmed("Boat operation returned an oversized result")
        try:
            single = json.loads(output)
        except ValueError:
            single = None
        if isinstance(single, dict) and "event" not in single:
            return single
        try:
            lines = output.splitlines()
            if operation not in ("new", "resume") or len(lines) > 128:
                raise ValueError
            rows = [single] if isinstance(single, dict) else [json.loads(line) for line in lines if line.strip()]
            initial = "created" if operation == "new" else "accepted"
            if not rows or any(not isinstance(row, dict) or row.get("event") not in (initial, "state", "ready")
                               or not isinstance(row.get("id"), str) or not BOX.fullmatch(row["id"]) for row in rows):
                raise ValueError
            if (rows[-1].get("event") != "ready" or machine_status(rows[-1].get("state")) != "available"
                    or any(row["id"] != rows[-1]["id"] or row["event"] == "ready" for row in rows[:-1])
                    or any(row["event"] == initial for row in rows[1:])):
                raise ValueError
            return rows[-1]
        except (ValueError, TypeError):
            raise Unconfirmed("Boat operation returned no complete identifiable result") from None

    def _call(self, args, *, timeout=180):
        delimiter = args.index("--") if "--" in args else len(args)
        command = [self.executable, *args[:delimiter], "--json", "--no-update", *args[delimiter:]]
        try:
            result = subprocess.run(command,
                                    capture_output=True, text=True, check=False, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            raise Unconfirmed("Boat operation did not return a confirmed result") from None
        if result.returncode:
            raise Unconfirmed("Boat operation did not return a confirmed result")
        return self._receipt(result.stdout, args[0])

    def credentials(self, profile):
        """Read only the service account from a cloud environment, before allocating compute."""
        if self.service_account_file is not None:
            try:
                descriptor = os.open(self.service_account_file, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor) as stream:
                    metadata = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600
                            or metadata.st_uid != os.getuid() or metadata.st_size > 128_000):
                        raise ValueError
                    raw = stream.read(128_001).strip()
                if raw.startswith("{"):
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) != {"service_account"}:
                        raise ValueError
                    raw = value["service_account"]
                if not isinstance(raw, str) or not raw or any(c.isspace() for c in raw) or "\x00" in raw:
                    raise ValueError
                return {"service_account": raw}
            except (OSError, ValueError, TypeError):
                raise WorkerError("cloud service-account file must contain one private valid token") from None
        raw = self._call(["env", "info", profile["credential_environment"]])
        try:
            contents = raw.get("environment", raw)["envContents"]
            tokens = []
            for line in contents.splitlines():
                key, separator, value = line.removeprefix("export ").partition("=")
                if separator and key.strip() == "OP_SERVICE_ACCOUNT_TOKEN":
                    parts = shlex.split(value, comments=True)
                    if len(parts) == 1 and parts[0] and not any(c.isspace() for c in parts[0]):
                        tokens.append(parts[0])
            if len(tokens) != 1:
                raise ValueError
            return {"service_account": tokens[0]}
        except (AttributeError, KeyError, TypeError, ValueError):
            raise WorkerError("cloud credential environment has no unique service account") from None

    def preflight(self, profile):
        """Native provider logins require a dedicated environment with personal access disabled."""
        if not profile["boat_agent_credentials"]:
            return {"agent_credentials": False}
        if profile["environment"] in ("base", "claudine"):
            raise PreflightRefused("native agent credentials require a dedicated worker environment")
        raw = self._call(["env", "info", profile["environment"]])
        row = raw.get("environment", raw) if isinstance(raw, dict) else {}
        effective = row.get("effective")
        expected = {"passGithub": False, "passSecrets": False, "passSandboxCredentials": False, "passAgentsCredentials": True}
        if (not isinstance(effective, dict) or any(effective.get(key) is not value for key, value in expected.items())
                or row.get("safeForThirdParties") is not False or row.get("proxyAiCredentials") is not False):
            raise PreflightRefused("worker environment must pass only native agent credentials; proxy mode requires separate configuration")
        return {"agent_credentials": True, "environment": profile["environment"], "effective": expected,
                "safeForThirdParties": False, "proxyAiCredentials": False}

    def create(self, profile, pool_id):
        if profile["boat_agent_credentials"]:
            self.preflight(profile)
        result = self._call(["new", "--environment", profile["environment"], *([] if profile["boat_agent_credentials"] else ["--no-env"]),
                             "--type", profile["type"], "--env", f"ONE_CONV_POOL_ID={pool_id}"])
        box = result.get("sandbox", result).get("id") if isinstance(result, dict) else None
        if not isinstance(box, str) or not BOX.fullmatch(box):
            raise Unconfirmed("Boat created no identifiable sandbox receipt")
        return box

    def info(self, box, *, timeout=180):
        result = self._call(["info", box], timeout=timeout)
        row = result.get("sandbox", result) if isinstance(result, dict) else {}
        if row.get("id") != box:
            raise WorkerError("Boat did not confirm the selected sandbox")
        return row

    def _transition(self, box, operation, wanted):
        """Confirm a known machine after one action; missing acknowledgements never repeat it."""
        try:
            self._call([operation, box])
        except Unconfirmed:
            pass  # A read of the tracked machine can confirm an ambiguous acknowledgement.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            state = self.info(box, timeout=max(1, remaining)).get("state")
            if machine_status(state) == wanted:
                return
            if state not in ("ready", "idle", "running", "archived", "stopped", "archiving", "stopping",
                             "provisioning", "provisioned", "starting", "cloning"):
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise Unconfirmed("Boat pool transition is not confirmed")

    def stop_pool(self, box):
        self._transition(box, "stop", "stopped")

    def resume_pool(self, box):
        self._transition(box, "resume", "available")

    def _exec(self, box, command):
        """Only fixed helper code and controller-generated hexadecimal paths cross Boat's shell."""
        result = self._call(["exec", box, "--timeout", "600", "--", shlex.join(command)], timeout=620)
        if (not isinstance(result, dict) or result.get("success") is not True or result.get("exitCode") != 0
                or any(result.get(key) is not False for key in ("stdoutTruncated", "timedOut", "launcherFailed", "oomKilled"))):
            raise Unconfirmed("worker helper did not return a complete execution receipt")
        return result.get("stdout", "")

    def bootstrap(self, box, payload):
        remote = f"/tmp/oneconv-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="oneconv-worker-") as folder:
            path = Path(folder) / "payload.json"
            private_json(path, payload)
            self._exec(box, ["python3", "-c", "import os,sys; os.mkdir(sys.argv[1],0o700)", remote])
            try:
                self._call(["scp", str(Path(__file__).with_name("worker_bootstrap.py")), f"{box}:{remote}/helper.py"])
                self._call(["scp", str(path), f"{box}:{remote}/payload.json"])
                output = self._exec(box, ["python3", f"{remote}/helper.py", f"{remote}/payload.json"])
                try:
                    health = json.loads(output)
                except ValueError:
                    raise Unconfirmed("worker helper returned unreadable health") from None
                return health
            finally:
                path.unlink(missing_ok=True)
                try:
                    self._exec(box, ["python3", "-c", "import shutil,sys; shutil.rmtree(sys.argv[1],ignore_errors=True)", remote])
                except WorkerError:
                    pass  # Cleanup cannot turn a confirmed launch into permission to launch again.


class WorkerManager:
    """Persist requests immediately and advance them from one background polling loop."""

    def __init__(self, directory: Path, config: dict):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.config = config
        profiles = config.get("profiles", {"default": {}})
        if not isinstance(profiles, dict):
            raise TypeError("profiles must be a named mapping")
        self.profiles = {name: _profile(value) for name, value in profiles.items()}
        if not self.profiles or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in self.profiles):
            raise ValueError("at least one named worker profile is required")
        self.boat = config.get("boat") or BoatCLI(config.get("boat_executable", "box"), config.get("service_account_file"))
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self):
        with self._thread_lock, (self.directory / ".state.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @property
    def state_path(self):
        return self.directory / "workers.json"

    def _save(self, state):
        private_json(self.state_path, state)

    def _spec(self, spec):
        if not isinstance(spec, dict) or set(spec) != {"name", "harness", "model", "topic", "profile", "prompt"}:
            raise ValueError("spec requires name, harness, model, topic, profile and prompt")
        if (not isinstance(spec["name"], str) or not NAME.fullmatch(spec["name"])
                or not isinstance(spec["topic"], str) or not NAME.fullmatch(spec["topic"])):
            raise ValueError("worker name and topic must use lowercase letters, digits and dashes")
        if (spec["harness"] not in ("claude", "codex", "omp") or not isinstance(spec["profile"], str)
                or spec["profile"] not in self.profiles):
            raise ValueError("unsupported harness or worker profile")
        model = spec["model"]
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 256
                                  or any(ord(c) < 32 for c in model) or model.startswith("-")):
            raise ValueError("model must be a nonempty model identifier or null")
        if not isinstance(spec["prompt"], str) or not spec["prompt"].strip() or len(spec["prompt"].encode()) > 128_000 or "\x00" in spec["prompt"]:
            raise ValueError("prompt must contain between 1 and 128000 UTF-8 bytes")
        return copy.deepcopy(spec)

    def submit(self, spec, id):
        if not isinstance(id, str) or not IDENTIFIER.fullmatch(id):
            raise ValueError("job id must use 1-64 letters, digits, underscores or dashes")
        spec = self._spec(spec)
        with self._locked():
            state = _read(self.state_path)
            if id in state["jobs"]:
                if state["jobs"][id]["spec"] != spec:
                    raise ValueError("job id is already bound to a different spec")
                return copy.deepcopy(state["jobs"][id])
            if any(job["spec"]["name"] == spec["name"] and job["status"] in ACTIVE for job in state["jobs"].values()):
                raise ValueError("worker name already has an active job")
            now = time.time()
            job = {"id": id, "status": "queued", "spec": spec, "created": now, "updated": now,
                   "pool_id": None, "box_id": None, "address": None, "phase": "queued"}
            state["jobs"][id] = job
            self._save(state)
            return copy.deepcopy(job)

    def status(self, id):
        with self._locked():
            try:
                return copy.deepcopy(_read(self.state_path)["jobs"][id])
            except KeyError:
                raise KeyError("unknown worker job") from None

    def list(self):
        with self._locked():
            state = _read(self.state_path)
            return {"jobs": copy.deepcopy(list(state["jobs"].values())), "pools": copy.deepcopy(list(state["pools"].values())),
                    "profiles": {name: {**copy.deepcopy(value), "active_capacity": "unverified"}
                                 for name, value in self.profiles.items()}}

    def _update(self, id, **fields):
        with self._locked():
            state = _read(self.state_path)
            state["jobs"][id].update(fields, updated=time.time())
            self._save(state)
            return copy.deepcopy(state["jobs"][id])

    def _pool(self, job):
        with self._locked():
            state = _read(self.state_path)
            profile = self.profiles[job["spec"]["profile"]]
            if job.get("pool_id"):
                return copy.deepcopy(state["pools"][job["pool_id"]])
            for pool in state["pools"].values():
                occupied = sum(item.get("pool_id") == pool["id"] and item["status"] in ACTIVE
                               for item in state["jobs"].values())
                if pool["profile"] == job["spec"]["profile"] and pool["status"] in ("available", "stopped") and occupied < pool["slots"]:
                    break
            else:
                ident = uuid.uuid4().hex
                pool = {"id": ident, "node": f"worker-{ident[:16]}", "profile": job["spec"]["profile"],
                        "slots": profile["slots"], "type": profile["type"], "status": "allocated",
                        "box_id": None, "created": time.time(), "active_capacity": "unverified"}
                state["pools"][ident] = pool
            state["jobs"][job["id"]].update(pool_id=pool["id"], box_id=pool["box_id"])
            self._save(state)
            return copy.deepcopy(pool)

    def _token(self, pool):
        path = self.directory / "tokens" / f"{pool['id']}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())["token"]
            except (OSError, ValueError, KeyError):
                raise WorkerError("worker node credential is unreadable") from None
        create = self.config.get("create_node_token")
        hub = self.config.get("hub")
        if create is None and hub is not None:
            create = hub.add_token
        if create is None:
            raise WorkerError("worker node token creation is not configured")
        token = create(pool["node"])
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise WorkerError("worker node token creation returned an invalid credential")
        private_json(path, {"token": token})
        return token

    def _hub_health(self, pool, job, health, token):
        address = health.get("address")
        if not isinstance(address, str) or not address.startswith(job["spec"]["harness"] + ":") or "/" in address:
            return False
        client = HubClient(self.config["public_hub_url"], token, pool["node"])
        qualified = f"{pool['node']}/{address}"
        sessions = client.request("GET", "/v1/sessions").get("sessions", [])
        published = any(row.get("address") == qualified and row.get("online") is True
                        and row.get("name") == job["spec"]["name"] for row in sessions)
        joined = any(row.get("topic") == job["spec"]["topic"] and qualified in row.get("members", []) for row in client.topics())
        return published and joined

    def _inspect_pool(self, pool, job=None):
        """Read tracked machine state before reuse; automatic stops invalidate every old process."""
        info = self.boat.info(pool["box_id"])
        machine = info.get("state") if isinstance(info, dict) else None
        status = machine_status(machine)
        if status is None:
            raise Unconfirmed("tracked pool machine state is unconfirmed")
        with self._locked():
            state = _read(self.state_path)
            actual = state["pools"][pool["id"]]
            actual["status"] = status
            actual["provider_state"] = machine
            actual["checked_at"] = time.time()
            if status == "stopped":
                for previous in state["jobs"].values():
                    if previous.get("pool_id") == pool["id"] and previous["status"] in ACTIVE:
                        previous.update(address=None, health={"registered": False, "bridge": False, "published": False,
                                                               "joined": False, "collector": False}, updated=time.time())
                        if job is not None and previous["id"] == job["id"]:
                            previous["restart"] = True
                            job["restart"] = True
                            job["address"] = None
                        else:
                            previous.update(status="stopped", phase="pool-stopped")
            self._save(state)
            return copy.deepcopy(actual)

    def _provision(self, job):
        profile = self.profiles[job["spec"]["profile"]]
        check_url(self.config.get("public_hub_url", ""))
        preflight = self.boat.preflight(profile) if hasattr(self.boat, "preflight") else {}
        credentials = self.boat.credentials(profile)
        pool = self._pool(job)
        token = self._token(pool)
        if pool["box_id"]:
            self._update(job["id"], phase="inspecting")
            pool = self._inspect_pool(pool, job)
        if pool["status"] == "stopped":
            self._update(job["id"], phase="resuming")
            with self._locked():
                state = _read(self.state_path)
                state["pools"][pool["id"]]["status"] = "resuming"
                self._save(state)
            self.boat.resume_pool(pool["box_id"])
            with self._locked():
                state = _read(self.state_path)
                state["pools"][pool["id"]]["status"] = "available"
                self._save(state)
            pool["status"] = "available"
        if not pool["box_id"]:
            self._update(job["id"], phase="creating")
            box = self.boat.create(profile, pool["id"])
            with self._locked():
                state = _read(self.state_path)
                state["pools"][pool["id"]].update(box_id=box, status="available")
                state["pools"][pool["id"]]["environment_health"] = preflight
                state["jobs"][job["id"]].update(box_id=box, phase="bootstrapping")
                self._save(state)
            pool.update(box_id=box, status="available")
        self._update(job["id"], phase="bootstrapping", box_id=pool["box_id"])
        payload = {"operation": "start", "job_id": job["id"], "spec": job["spec"], "profile": profile,
                   "pool_id": pool["id"], "hub": {"url": self.config["public_hub_url"], "node": pool["node"], "token": token},
                   "restart": job.get("restart", False), **credentials}
        health = self.boat.bootstrap(pool["box_id"], payload)
        if not isinstance(health, dict) or health.get("status") not in ("ready", "auth_needed", "unverified", "failed", "unconfirmed"):
            raise Unconfirmed("worker returned no valid health receipt")
        if health["status"] == "failed" and job.get("address"):
            health = {**health, "status": "unconfirmed"}
        public = {key: health[key] is True for key in ("authenticated", "registered", "model_verified", "bridge", "published", "joined", "collector") if key in health}
        if isinstance(health.get("stage"), str) and re.fullmatch(r"[a-z-]{1,48}", health["stage"]):
            public["stage"] = health["stage"]
        if isinstance(health.get("address"), str) and re.fullmatch(r"(?:claude|codex|omp):[A-Za-z0-9._-]{1,128}", health["address"]):
            public["address"] = health["address"]
        if isinstance(health.get("task_id"), str) and re.fullmatch(r"[0-9a-f]{32}", health["task_id"]):
            public["task_id"] = health["task_id"]
        if health["status"] == "ready" and (
                any(health.get(key) is not True for key in ("authenticated", "registered", "model_verified", "bridge", "published", "joined", "collector"))
                or "task_id" not in public
                or not self._hub_health(pool, job, health, token)):
            raise Unconfirmed("worker readiness was not confirmed by the live hub")
        self._update(job["id"], status=health["status"], phase=health["status"], health=public,
                     address=f"{pool['node']}/{public['address']}" if public.get("address") else
                     job.get("address") if health["status"] == "unconfirmed" else None, error=None,
                     restart=False if health["status"] == "ready" else job.get("restart", False))

    def run_once(self):
        """Advance one queued job; a released claim is ambiguous after a controller crash."""
        with (self.directory / ".runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            with self._locked():
                state = _read(self.state_path)
                for job in state["jobs"].values():
                    if job["status"] == "provisioning":
                        ambiguous = job["phase"] in ("creating", "bootstrapping", "resuming", "stopping") or bool(job.get("address"))
                        job.update(status="unconfirmed" if ambiguous else "failed",
                                   error="controller stopped during a provider operation", updated=time.time())
                job = next((item for item in state["jobs"].values() if item["status"] == "queued"), None)
                if job is not None:
                    job.update(status="provisioning", updated=time.time())
                self._save(state)
                job = copy.deepcopy(job)
            if job is None:
                return None
            try:
                self._provision(job)
            except PreflightRefused:
                current = self.status(job["id"])
                self._update(job["id"], status="unconfirmed" if current.get("address") else "failed", phase="preflight",
                             error="worker environment preflight refused allocation")
            except Unconfirmed:
                current = self.status(job["id"])
                status = "unconfirmed" if current["phase"] in ("creating", "bootstrapping", "resuming") or current.get("address") else "failed"
                self._update(job["id"], status=status, error="provider outcome is unconfirmed; reconcile before retrying" if status == "unconfirmed" else "cloud credential read failed; no sandbox was created")
            except (OSError, ValueError, TypeError, KeyError, WorkerError, HubUnavailable, HubRefused):
                # Provider exceptions may include credentials, commands or user text.
                current = self.status(job["id"])
                status = "unconfirmed" if current["phase"] in ("creating", "bootstrapping", "resuming") or current.get("address") else "failed"
                self._update(job["id"], status=status, error="worker operation failed; provider diagnostics withheld")
            return self.status(job["id"])

    def retry(self, id):
        """Recheck known-box authentication without replacing its pool or agent identity."""
        with self._locked():
            state = _read(self.state_path)
            job = state["jobs"][id]
            recheck = (job["status"] == "unconfirmed" and job.get("box_id")
                       and job["phase"] in ("bootstrapping", "preflight", "inspecting", "resuming", "unconfirmed", "pool-unconfirmed"))
            restart = job["status"] == "stopped" and job["phase"] == "pool-stopped"
            ready = job["status"] == "ready" and job.get("box_id")
            if job["status"] not in ("auth_needed", "unverified", "failed") and not recheck and not restart and not ready:
                raise ValueError("only known safe setup or a known-box readiness check can be retried")
            if restart or ready:
                pool = state["pools"][job["pool_id"]]
                if pool["status"] != "available":
                    raise ValueError("resume the worker pool before rechecking its agents")
                job["restart"] = True
            job.update(status="queued", error=None, updated=time.time())
            self._save(state)
            return copy.deepcopy(job)

    recheck = retry

    def reconcile(self, id, box_id):
        """Attach an explicitly inspected sandbox after a lost create acknowledgement."""
        if not isinstance(box_id, str) or not BOX.fullmatch(box_id):
            raise ValueError("invalid Boat sandbox id")
        self.boat.info(box_id)
        with self._locked():
            state = _read(self.state_path)
            job = state["jobs"][id]
            if job["status"] != "unconfirmed" or job["phase"] != "creating" or not job.get("pool_id"):
                raise ValueError("only an unconfirmed sandbox create can be reconciled")
            if any(pool["box_id"] == box_id and pool["id"] != job["pool_id"] for pool in state["pools"].values()):
                raise ValueError("sandbox already belongs to another pool")
            pool = state["pools"][job["pool_id"]]
            pool.update(box_id=box_id, status="available")
            job.update(box_id=box_id, status="queued", phase="reconciled", error=None, updated=time.time())
            self._save(state)
            return copy.deepcopy(job)

    def stop(self, id):
        """Stop only this native worker; a shared sandbox remains available to other workers."""
        with (self.directory / ".runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkerError("worker provisioning is running; retry stop after its outcome is known") from None
            job = self.status(id)
            if job["status"] == "stopped":
                return job
            if job["status"] == "unconfirmed" and not job.get("box_id"):
                raise WorkerError("sandbox creation is unconfirmed; reconcile before stopping")
            if job.get("box_id"):
                self._update(id, status="provisioning", phase="stopping")
                try:
                    health = self.boat.bootstrap(job["box_id"], {"operation": "stop", "job_id": id, "pool_id": job["pool_id"]})
                    if health != {"status": "stopped"}:
                        raise Unconfirmed("native worker stop was not confirmed")
                except (OSError, ValueError, TypeError, KeyError, WorkerError):
                    return self._update(id, status="unconfirmed", error="worker stop is unconfirmed")
            return self._update(id, status="stopped", phase="stopped", address=None, error=None,
                                health={"registered": False, "bridge": False, "published": False, "joined": False, "collector": False})

    def _pool_action(self, pool_id, resume):
        with (self.directory / ".runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkerError("worker provisioning is running; retry the pool action afterward") from None
            with self._locked():
                state = _read(self.state_path)
                if pool_id not in state["pools"] or not state["pools"][pool_id].get("box_id"):
                    raise ValueError("pool has no tracked sandbox")
                pool = copy.deepcopy(state["pools"][pool_id])
            try:
                pool = self._inspect_pool(pool)
            except WorkerError:
                with self._locked():
                    state = _read(self.state_path)
                    state["pools"][pool_id]["status"] = "unconfirmed"
                    self._save(state)
                raise
            with self._locked():
                state = _read(self.state_path)
                pool = state["pools"][pool_id]
                if pool["status"] == ("available" if resume else "stopped"):
                    return copy.deepcopy(pool)
                if resume and pool["status"] != "stopped":
                    raise ValueError("only a confirmed stopped pool can be resumed")
                pool["status"] = "resuming" if resume else "stopping"
                self._save(state)
                box_id = pool["box_id"]
            confirmed = False
            try:
                (self.boat.resume_pool if resume else self.boat.stop_pool)(box_id)
                confirmed = True
            except (OSError, ValueError, TypeError, KeyError, WorkerError):
                pass
            with self._locked():
                state = _read(self.state_path)
                pool = state["pools"][pool_id]
                pool["status"] = ("available" if resume else "stopped") if confirmed else "unconfirmed"
                for job in state["jobs"].values():
                    if job.get("pool_id") == pool_id and job["status"] in ACTIVE:
                        job.update(status="stopped" if confirmed and not resume else "unconfirmed",
                                   phase="pool-stopped" if confirmed and not resume else "pool-unconfirmed",
                                   address=None, health={"registered": False, "bridge": False, "published": False,
                                                         "joined": False, "collector": False}, updated=time.time())
                self._save(state)
                return copy.deepcopy(pool)

    def stop_pool(self, pool_id):
        """Snapshot and stop only a tracked pool, retaining its disk and job records."""
        return self._pool_action(pool_id, False)

    def resume_pool(self, pool_id):
        """Resume a tracked snapshot without declaring any old native process ready."""
        return self._pool_action(pool_id, True)
