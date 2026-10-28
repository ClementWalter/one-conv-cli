"""The Boat client and box profiles the controller builds the cloud spawner box with.

An ambiguous provider acknowledgement raises Unconfirmed rather than being retried blindly.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
BOX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
DEFAULT_PROFILE = {
    "repositories": ["ClementWalter/claudine", "ClementWalter/one-conv-cli"],
    "type": "default", "credential_environment": "claudine", "environment": "base",
    "boat_agent_credentials": False,
}
# Profile fields only per-job cloud workers used; a config that still has them loads, and they are dropped.
RETIRED_PROFILE_FIELDS = {"work_repository", "skills", "slots", "probe_enabled", "credential_refs", "billing_scope",
                          "revisions", "harness_args"}


class WorkerError(RuntimeError):
    """A safe operation label without provider output or credential values."""


class Unconfirmed(WorkerError):
    """A side effect may have happened and must not be repeated blindly."""


class Gone(WorkerError):
    """Boat no longer knows the sandbox: it was deleted."""


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


def _repository(value: str) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value)
            is not None and value.split("/")[1] not in (".", "..") and not value.endswith(".git"))


def _profile(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_PROFILE) - RETIRED_PROFILE_FIELDS:
        raise ValueError("unsupported box profile fields")
    profile = {**DEFAULT_PROFILE, **{key: item for key, item in value.items() if key in DEFAULT_PROFILE}}
    repos = profile["repositories"]
    if (not isinstance(repos, list) or not repos or not all(_repository(repo) for repo in repos)
            or len({repo.split("/")[1].lower() for repo in repos}) != len(repos)
            or "ClementWalter/one-conv-cli" not in repos):
        raise ValueError("profile repositories must be distinct GitHub repositories including one-conv-cli")
    if profile["type"] not in ("small", "default", "large"):
        raise ValueError("unsupported box machine type")
    if type(profile["boat_agent_credentials"]) is not bool:
        raise ValueError("boat_agent_credentials must be a boolean")
    for key in ("environment", "credential_environment"):
        if not isinstance(profile[key], str) or not NAME.fullmatch(profile[key]):
            raise ValueError("environment must be a named Boat environment")
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
            try:
                gone = json.loads(result.stdout).get("code") == "not_found"
            except (ValueError, AttributeError):
                gone = False
            if gone:
                raise Gone("Boat does not know this sandbox")
            raise Unconfirmed("Boat operation did not return a confirmed result")
        if args[0] == "scp":
            return {}  # SCP acknowledges transport by exit status; helper execution checks the files.
        return self._receipt(result.stdout, args[0])

    def credentials(self, profile):
        """Read only the service account from a cloud environment, before allocating compute."""
        if self.service_account_file is not None:
            try:
                descriptor = os.open(self.service_account_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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

    def create(self, profile, pool_id, *, auto_stop=True):
        """`auto_stop=False` for a box whose owner stops it: Boat's auto-stop is a wall-clock TTL that kills busy work."""
        if profile["boat_agent_credentials"]:
            self.preflight(profile)
        result = self._call(["new", "--environment", profile["environment"], *([] if profile["boat_agent_credentials"] else ["--no-env"]),
                             "--type", profile["type"], *([] if auto_stop else ["--no-auto-stop"]),
                             "--env", f"ONE_CONV_POOL_ID={pool_id}"])
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


class CloudBox:
    """The controller's private state for the spawner box: its Boat client, box profiles and node token."""

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
            raise ValueError("at least one named box profile is required")
        self.boat = config.get("boat") or BoatCLI(config.get("boat_executable", "box"), config.get("service_account_file"))

    def pool_token_path(self, node: str) -> Path | None:
        """Where the token a former worker pool on `node` holds is kept, so the box keeps its node identity."""
        try:
            pools = json.loads((self.directory / "workers.json").read_text())["pools"].values()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        pool = next((pool for pool in pools if isinstance(pool, dict) and pool.get("node") == node), None)
        return self.directory / "tokens" / f"{pool['id']}.json" if pool and isinstance(pool.get("id"), str) else None
