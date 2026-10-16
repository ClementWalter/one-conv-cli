"""Provision resident worker boxes without duplicating sandboxes or claiming unverified capacity.

WorkerManager is the callable the authenticated cloud API wires: submit, status, list, stop.
A job id is persisted in queued before any Boat command. Lost create acks become unknown and
are reconciled; they never blindly create a second sandbox.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import worker_bootstrap as bootstrap

STATES = ("queued", "provisioning", "ready", "failed", "unknown")
JOB_ID = re.compile(r"[0-9a-f]{32}")
SANDBOX = re.compile(r"bx_[A-Za-z0-9]+")
MAX_CAP = 10
DEFAULT_CAP = 4
IDLE_RESIDENCY_VERIFIED_UP_TO = 10
HELPER = Path(__file__).with_name("worker_bootstrap.py")
REMOTE_HELPER = f"/tmp/{bootstrap.FIXED_HELPER_NAME}"


class WorkerError(ValueError):
    """A worker job or spec that cannot be interpreted without guessing."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _job_id(value=None) -> str:
    if value is None:
        return uuid.uuid4().hex
    if not isinstance(value, str) or not JOB_ID.fullmatch(value):
        raise WorkerError("job id must be 32 lowercase hex characters")
    return value


def normalize_config(config) -> dict:
    config = {} if config is None else dict(config)
    cap = config.get("max_agents_per_box", DEFAULT_CAP)
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= MAX_CAP:
        raise WorkerError("max_agents_per_box must be an integer from 1 to 10")
    environment = config.get("environment", "claudine")
    if not isinstance(environment, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", environment):
        raise WorkerError("invalid Boat environment name")
    box_type = config.get("box_type", "default")
    if box_type not in ("small", "default", "large", "xlarge"):
        raise WorkerError("box_type must be small, default, large or xlarge")
    ttl = config.get("ttl")
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1):
        raise WorkerError("ttl must be a positive integer of seconds")
    return {
        "environment": environment,
        "credential_environment": config.get("credential_environment", environment),
        "box_type": box_type,
        "max_agents_per_box": cap,
        "allow_create": bool(config.get("allow_create", True)),
        "ttl": ttl,
        "hub_url": config.get("hub_url"),
        "node": config.get("node"),
        "token": config.get("token"),
        "service_account": config.get("service_account"),
    }


def normalize_spec(spec, *, cap: int) -> dict:
    if not isinstance(spec, dict):
        raise WorkerError("spec must be an object")
    extra = set(spec) - {"repo", "repositories", "primary_repo", "agents", "sandbox_id"}
    if extra:
        raise WorkerError("unexpected spec fields")
    repos = spec.get("repositories")
    if repos is None and spec.get("repo"):
        repos = [spec["repo"]]
    try:
        repositories = bootstrap.repositories(repos)
    except bootstrap.BootstrapError as error:
        raise WorkerError(str(error)) from error
    primary = spec.get("primary_repo") or spec.get("repo") or repositories[0]
    if primary not in repositories:
        raise WorkerError("primary_repo must be one of the selected repositories")
    agents = spec.get("agents")
    if not isinstance(agents, list) or not agents:
        raise WorkerError("agents must be a non-empty list")
    if len(agents) > cap:
        raise WorkerError(f"this box cap is {cap} resident agents; idle residency is verified up to 10")
    try:
        agents = [bootstrap.validate_agent(agent) for agent in agents]
    except bootstrap.BootstrapError as error:
        raise WorkerError(str(error)) from error
    if len({agent["name"] for agent in agents}) != len(agents):
        raise WorkerError("agent names must be unique on a box")
    sandbox_id = spec.get("sandbox_id")
    if sandbox_id is not None and (not isinstance(sandbox_id, str) or not SANDBOX.fullmatch(sandbox_id)):
        raise WorkerError("sandbox_id must look like bx_...")
    return {
        "repositories": repositories, "primary_repo": primary, "agents": agents,
        **({} if sandbox_id is None else {"sandbox_id": sandbox_id}),
    }


def rate_cards(spec) -> list[str]:
    return [agent["cost_center_id"] for agent in spec["agents"] if agent.get("cost_center_id")]


def capacity_metadata(cap: int) -> dict:
    return {
        "cap": cap,
        "idle_residency_verified_up_to": IDLE_RESIDENCY_VERIFIED_UP_TO,
        "active_capacity_verified": False,
        "note": "Idle residency of 10 native Claude processes was measured; concurrent model turns are unverified.",
    }


def public_job(job: dict) -> dict:
    """Drop secrets. Callers and logs must use this view."""
    return {key: job[key] for key in (
        "id", "state", "spec", "sandbox_id", "create_started", "agents", "health",
        "quota", "rate_cards", "capacity", "auth_needed", "error", "created_at",
        "updated_at", "stopped",
    ) if key in job}


def extract_created_id(text: str) -> str | None:
    """First sandbox id from `box new --json` JSONL, or None if creation was not acknowledged."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ident = row.get("id")
        if row.get("event") == "created" and isinstance(ident, str) and SANDBOX.fullmatch(ident):
            return ident
        nested = (row.get("sandbox") or row.get("data") or {})
        ident = nested.get("id") if isinstance(nested, dict) else None
        if isinstance(ident, str) and SANDBOX.fullmatch(ident):
            return ident
    return None


def _ready(job: dict) -> bool:
    health = job.get("health") or {}
    agents = health.get("agents") or job.get("agents") or []
    if not agents or job.get("auth_needed") or job.get("stopped"):
        return False
    bridge = health.get("bridge") or {}
    if not bridge.get("running") or not bridge.get("published"):
        return False
    return all(agent.get("registered") and agent.get("published") and agent.get("topic") in agent.get("topics", [])
               and agent.get("auth") == "configured" and agent.get("address")
               and "/" not in str(agent.get("address")) for agent in agents)


class JobStore:
    """Durable jobs in a private SQLite file. Reads do not create the registry."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "workers.sqlite3"

    def _connect(self, create=True):
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def get(self, job_id: str) -> dict | None:
        if not self.path.exists():
            return None
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                row = connection.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        except sqlite3.Error as error:
            raise WorkerError("Cannot read the worker registry") from error
        if row is None:
            return None
        job = json.loads(row[0])
        if job.get("id") != job_id:
            raise WorkerError("worker registry identity mismatch")
        return job

    def list(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                rows = connection.execute("SELECT id, payload FROM jobs ORDER BY id").fetchall()
        except sqlite3.Error as error:
            raise WorkerError("Cannot read the worker registry") from error
        jobs = []
        for job_id, payload in rows:
            job = json.loads(payload)
            if job.get("id") != job_id:
                raise WorkerError("worker registry identity mismatch")
            jobs.append(job)
        return jobs

    def put(self, job: dict) -> dict:
        job = {**job, "updated_at": _now()}
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                connection.execute("INSERT INTO jobs VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                                   (job["id"], json.dumps(job)))
        except sqlite3.Error as error:
            raise WorkerError("Cannot write the worker registry") from error
        return job


class BoxProvider:
    """Boat operations used by WorkerManager. Tests inject a fake; production uses BoatBox."""

    def create(self, *, environment, box_type, job_id, ttl=None, on_id=None) -> str:
        raise WorkerError("no box provider")

    def list(self) -> list[dict]:
        return []

    def limits(self) -> dict:
        return {}

    def scp(self, sandbox: str, local: Path, remote: str) -> None:
        raise WorkerError("no box provider")

    def exec(self, sandbox: str, argv: list[str], timeout: int = 120) -> str:
        raise WorkerError("no box provider")

    def stop(self, sandbox: str) -> None:
        raise WorkerError("no box provider")

    def env_info(self, environment: str) -> dict:
        return {}

    def job_id_for(self, sandbox: str) -> str | None:
        return None


class BoatBox(BoxProvider):
    """`box` CLI adapter. Creation streams JSONL so a sandbox id is captured before the process ends."""

    def __init__(self, run=None):
        self._run = run

    def _box(self, args: list[str], timeout=120, text=True):
        import subprocess
        try:
            return subprocess.run(["box", *args, "--json", "--no-update"], capture_output=True,
                                  text=text, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkerError("box command failed") from error

    def create(self, *, environment, box_type, job_id, ttl=None, on_id=None) -> str:
        import subprocess
        args = ["box", "new", "--environment", environment, "--type", box_type, "--json", "--no-update",
                "--env", f"ONE_CONV_WORKER_JOB={job_id}"]
        if ttl:
            args.extend(["--ttl", str(ttl)])
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as error:
            raise WorkerError("box command failed") from error
        captured, chunks = None, []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                chunks.append(line)
                ident = extract_created_id(line)
                if ident and captured is None:
                    captured = ident
                    if on_id is not None:
                        on_id(ident)
            process.wait(timeout=900)
        except (OSError, subprocess.SubprocessError) as error:
            process.kill()
            if captured is None:
                raise WorkerError("sandbox create was not acknowledged") from error
            return captured
        if captured is None:
            captured = extract_created_id("".join(chunks))
            if captured and on_id is not None:
                on_id(captured)
        if captured is None:
            raise WorkerError("sandbox create was not acknowledged")
        return captured

    def list(self) -> list[dict]:
        result = self._box(["list", "--all"], timeout=60)
        if result.returncode:
            raise WorkerError("box list failed")
        rows = []
        for line in result.stdout.splitlines():
            if not line.strip().startswith("{"):
                continue
            payload = json.loads(line)
            boxes = payload.get("sandboxes") or payload.get("boxes") or ([payload] if payload.get("id") else [])
            for row in boxes:
                ident = row.get("id")
                if isinstance(ident, str) and SANDBOX.fullmatch(ident):
                    rows.append({"id": ident, "name": row.get("name") or "", "state": row.get("state") or "",
                                 "job_id": (row.get("env") or {}).get("ONE_CONV_WORKER_JOB")})
        return rows

    def limits(self) -> dict:
        result = self._box(["limits"], timeout=30)
        if result.returncode:
            return {"available": False}
        try:
            data = json.loads(result.stdout.splitlines()[-1])
        except (ValueError, IndexError):
            return {"available": False}
        return {"available": True, "can_start": bool(data.get("canStart")),
                "credit_balance_hours": data.get("creditBalanceHours")}

    def scp(self, sandbox: str, local: Path, remote: str) -> None:
        result = self._box(["scp", str(local), f"{sandbox}:{remote}"], timeout=120)
        if result.returncode:
            raise WorkerError("box scp failed")

    def exec(self, sandbox: str, argv: list[str], timeout: int = 120) -> str:
        import subprocess
        command = bootstrap.box_exec_argv(sandbox, argv, timeout)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 30, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkerError("box exec failed") from error
        if result.returncode:
            raise WorkerError("box exec failed")
        return result.stdout

    def stop(self, sandbox: str) -> None:
        result = self._box(["stop", sandbox], timeout=120)
        if result.returncode:
            raise WorkerError("box stop failed")

    def job_id_for(self, sandbox: str) -> str | None:
        try:
            raw = self.exec(sandbox, ["printenv", "ONE_CONV_WORKER_JOB"], timeout=30)
        except WorkerError:
            return None
        try:
            data = json.loads(raw)
            text = data.get("stdout", raw) if isinstance(data, dict) else raw
        except ValueError:
            text = raw
        value = text.strip().splitlines()[-1] if text.strip() else ""
        return value if JOB_ID.fullmatch(value) else None


class WorkerManager:
    """Durable provision jobs for resident native tmux agents packed onto one Boat box."""

    def __init__(self, store_dir, config, provider: BoxProvider | None = None):
        self.config = normalize_config(config)
        self.store = JobStore(Path(store_dir))
        self.provider = provider if provider is not None else BoatBox()
        self.cap = self.config["max_agents_per_box"]

    def _new_job(self, spec, job_id) -> dict:
        return {
            "id": job_id, "state": "queued", "spec": spec, "sandbox_id": spec.get("sandbox_id"),
            "create_started": False, "agents": [], "health": None, "quota": None,
            "rate_cards": rate_cards(spec), "capacity": capacity_metadata(self.cap),
            "auth_needed": [], "error": None, "created_at": _now(), "updated_at": _now(),
            "stopped": False,
        }

    def submit(self, spec, id=None) -> dict:
        job_id = _job_id(id)
        spec = normalize_spec(spec, cap=self.cap)
        current = self.store.get(job_id)
        if current is not None:
            if current.get("spec") != spec:
                raise WorkerError("job id already has a different spec")
            return public_job(self._resume(current))
        job = self.store.put(self._new_job(spec, job_id))
        return public_job(self._provision(job))

    def status(self, id) -> dict:
        job = self.store.get(_job_id(id))
        if job is None:
            raise WorkerError("no worker job with that id")
        return public_job(job)

    def list(self) -> list[dict]:
        return [public_job(job) for job in self.store.list()]

    def stop(self, id) -> dict:
        job = self.store.get(_job_id(id))
        if job is None:
            raise WorkerError("no worker job with that id")
        if job.get("sandbox_id"):
            try:
                self.provider.stop(job["sandbox_id"])
            except WorkerError as error:
                job = self.store.put({**job, "state": "failed",
                                      "error": {"stage": "stop", "message": str(error)}})
                return public_job(job)
        job = self.store.put({**job, "state": "failed", "stopped": True,
                              "error": {"stage": "stop", "message": "worker stopped"}})
        return public_job(job)

    def _resume(self, job: dict) -> dict:
        if job["state"] in ("ready", "failed") or job.get("stopped"):
            return job
        if job["state"] == "unknown" or (job.get("create_started") and not job.get("sandbox_id")):
            return self._reconcile(job)
        return self._provision(job)

    def _reconcile(self, job: dict) -> dict:
        """Find a sandbox tagged with this job id. Never call create again."""
        owned = None
        for row in self.provider.list():
            if row.get("job_id") == job["id"] or self.provider.job_id_for(row["id"]) == job["id"]:
                if owned and owned != row["id"]:
                    return self.store.put({**job, "state": "unknown",
                                           "error": {"stage": "reconcile",
                                                     "message": "multiple sandboxes claim this job; inspect before creating another"}})
                owned = row["id"]
        if owned is None:
            return self.store.put({**job, "state": "unknown",
                                   "error": {"stage": "create",
                                             "message": "sandbox create was not acknowledged; not creating another"}})
        job = self.store.put({**job, "sandbox_id": owned, "state": "provisioning",
                              "error": None})
        return self._provision(job)

    def _capture_id(self, job: dict):
        def on_id(sandbox_id):
            stored = self.store.get(job["id"]) or job
            self.store.put({**stored, "sandbox_id": sandbox_id, "state": "provisioning"})
        return on_id

    def _create(self, job: dict) -> dict:
        if job.get("sandbox_id"):
            return job
        if job.get("create_started"):
            return self._reconcile(job)
        if not self.config["allow_create"]:
            return self.store.put({**job, "state": "failed",
                                   "error": {"stage": "create", "message": "creating a box is disabled; coordinate first"}})
        job = self.store.put({**job, "state": "provisioning", "create_started": True})
        try:
            sandbox_id = self.provider.create(
                environment=self.config["environment"], box_type=self.config["box_type"],
                job_id=job["id"], ttl=self.config["ttl"], on_id=self._capture_id(job))
        except WorkerError as error:
            stored = self.store.get(job["id"]) or job
            if stored.get("sandbox_id"):
                return stored
            return self.store.put({**stored, "state": "unknown",
                                   "error": {"stage": "create", "message": str(error)}})
        stored = self.store.get(job["id"]) or job
        return self.store.put({**stored, "sandbox_id": sandbox_id, "state": "provisioning"})

    def _payload(self, job: dict) -> dict:
        payload = {
            "job_id": job["id"],
            "repositories": job["spec"]["repositories"],
            "primary_repo": job["spec"]["primary_repo"],
            "agents": job["spec"]["agents"],
        }
        if self.config.get("hub_url"):
            payload["hub_url"] = self.config["hub_url"]
        if self.config.get("node"):
            payload["node"] = self.config["node"]
        if self.config.get("token"):
            payload["hub_token"] = self.config["token"]
        if self.config.get("service_account"):
            payload["service_account"] = self.config["service_account"]
        return bootstrap.validate_payload(payload)

    def _bootstrap(self, job: dict) -> dict:
        sandbox = job["sandbox_id"]
        payload = self._payload(job)
        remote_payload = f"/tmp/one-conv-worker-{job['id']}.json"
        with tempfile.TemporaryDirectory(prefix="one-conv-worker-") as temporary:
            local = Path(temporary) / "payload.json"
            fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle)
            try:
                self.provider.scp(sandbox, HELPER, REMOTE_HELPER)
                self.provider.scp(sandbox, local, remote_payload)
                raw = self.provider.exec(sandbox, ["python3", REMOTE_HELPER, "--worker-payload", remote_payload])
            finally:
                local.unlink(missing_ok=True)
                try:
                    self.provider.exec(sandbox, ["python3", "-c",
                                                 "import pathlib,sys; [pathlib.Path(p).unlink(missing_ok=True) for p in sys.argv[1:]]",
                                                 remote_payload, REMOTE_HELPER], timeout=30)
                except WorkerError:
                    pass
        public_payload = {key: payload[key] for key in payload if key not in bootstrap.SECRET_KEYS}
        return bootstrap.parse_exec_health(raw, {**public_payload, "job_id": payload["job_id"]})

    def _apply_health(self, job: dict, health: dict) -> dict:
        auth_needed = list(health.get("auth_needed") or [])
        agents = health.get("agents") or []
        job = {
            **job,
            "agents": agents,
            "health": health,
            "auth_needed": auth_needed,
            "rate_cards": rate_cards(job["spec"]),
            "capacity": {**capacity_metadata(self.cap), **(health.get("capacity") or {})},
        }
        if _ready(job):
            job["state"] = "ready"
            job["error"] = None
        else:
            job["state"] = "provisioning"
            if auth_needed:
                job["error"] = {"stage": "auth", "message": "auth-needed: " + ",".join(auth_needed)}
            else:
                job["error"] = {"stage": "health",
                                "message": "registration, publish and topic membership are required before ready"}
        return self.store.put(job)

    def _provision(self, job: dict) -> dict:
        if job.get("stopped"):
            return job
        quota = None
        try:
            quota = self.provider.limits()
        except WorkerError:
            quota = {"available": False}
        job = self.store.put({**job, "quota": quota})
        job = self._create(job)
        if job["state"] in ("failed", "unknown") or not job.get("sandbox_id"):
            return job
        try:
            health = self._bootstrap(job)
        except (WorkerError, bootstrap.BootstrapError, ValueError, OSError) as error:
            return self.store.put({**job, "state": "failed",
                                   "error": {"stage": "bootstrap", "message": bootstrap.redact(str(error))}})
        return self._apply_health(job, health)
