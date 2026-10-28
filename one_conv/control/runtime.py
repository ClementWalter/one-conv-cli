"""Wire cloud messaging, durable worker jobs and shared accounting into one service."""

import asyncio
import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from ..hr import RateError, RateStore, make_card, quote
from ..live.hub import LEASE_SECONDS, HubError

log = logging.getLogger(__name__)


def _fields(payload, required, optional=()):
    if not isinstance(payload, dict) or set(payload) - set(required) - set(optional) or set(required) - set(payload):
        raise ValueError("Request fields do not match this accounting operation")


class Accounting:
    """Nodes report their own workers; all teammates can inspect the shared accounts."""

    def __init__(self, directory):
        from ..accounting import Ledger
        self.rates = RateStore(Path(directory) / "hr")
        self.ledger = Ledger(Path(directory) / "accounting")

    @staticmethod
    def filters(payload, *, summary=False):
        allowed = {"cost_center_id", "since", "until", "occupied_seconds" if summary else "limit"}
        _fields(payload, (), allowed)
        result = dict(payload)
        if "limit" in result:
            if isinstance(result["limit"], bool):
                raise ValueError("limit must be an integer")
            result["limit"] = int(result["limit"])
        return result

    def teammate(self, spec, ident):
        """Ask the hub for one more session in the spec's channel; it starts on the preferred connected spawner."""
        from .store import NODE
        spec = self.manager._spec(spec)
        if not isinstance(ident, str):
            raise ValueError("Choose a request ID")
        return self.hub.request_teammate(NODE, "user", ident, spec["topic"], f"New teammate @{spec['name']}: {spec['prompt']}")

    def browser(self, method, payload):
        if method == "GET":
            return {"cards": self.rates.list(), "events": self.ledger.list_events(limit=100),
                    "summary": self.ledger.summary()}
        if payload.get("action") == "quote":
            _fields(payload, ("action", "reference", "minutes"))
            return quote(self.rates.show(payload["reference"]), payload["minutes"])
        if payload.get("action") == "register":
            _fields(payload, ("action", "card"), ("replace",))
            if type(payload.get("replace", False)) is not bool:
                raise ValueError("replace must be a boolean")
            # Browser tabs may hold stale identity fields after a worker joins or is renamed.
            if payload.get("replace", False):
                raise ValueError("Refresh the workspace to edit this card’s planning rate; browser registration cannot replace worker identity. API clients: use update_rate.")
            return {"card": self.rates.register(payload["card"])}
        if payload.get("action") == "update_rate":
            _fields(payload, ("action", "reference", "hourly_rate"))
            return {"card": self.rates.update_rate(payload["reference"], payload["hourly_rate"])}
        raise ValueError("Choose quote, register or update_rate")

    def node(self, node, resource, method, payload):
        from ..accounting import normalize_event
        if resource == "rates" and method == "GET":
            _fields(payload, ())
            return {"rates": self.rates.list()}
        if resource == "rates" and method == "POST":
            _fields(payload, ("card",), ("replace",))
            card = payload["card"]
            if not isinstance(card, dict) or not isinstance(card.get("addresses"), list) or not card["addresses"]:
                raise ValueError("Node rate cards require at least one own-node worker address")
            if any(not isinstance(value, str) or not value.startswith(node + "/") for value in card["addresses"]):
                raise HubError(403, "A node can register only its own worker addresses")
            if type(payload.get("replace", False)) is not bool:
                raise ValueError("replace must be a boolean")
            def authorize(current):
                if current and (not current["addresses"] or
                                any(not address.startswith(node + "/") for address in current["addresses"])):
                    raise HubError(403, "This cost center is managed by another node or the human")

            return {"card": self.rates.register(card, replace=payload.get("replace", False), authorize=authorize)}
        if resource == "quote" and method == "POST":
            _fields(payload, ("reference", "minutes"))
            return {"quote": quote(self.rates.show(payload["reference"]), payload["minutes"])}
        if resource == "usage" and method == "GET":
            return {"events": self.ledger.list_events(**self.filters(payload))}
        if resource == "summary" and method == "GET":
            return {"summary": self.ledger.summary(**self.filters(payload, summary=True))}
        if resource == "events" and method == "POST":
            _fields(payload, ("events",))
            events = payload["events"]
            if not isinstance(events, list) or not 1 <= len(events) <= 100:
                raise ValueError("Send between 1 and 100 usage events per batch")
            accepted = []
            for raw in events:
                event = normalize_event(raw)
                if event.get("node_id") not in (None, node):
                    raise HubError(403, "Usage node identity comes from the bearer token")
                if not event["worker_id"].startswith(node + "/") or (
                    event["parent_worker_id"] is not None and not event["parent_worker_id"].startswith(node + "/")
                ):
                    raise HubError(403, "A node can report only its own workers")
                event["node_id"] = node
                accepted.append(event)
            with self.rates.locked_cards() as cards:
                owners = {card["cost_center_id"]: card for card in cards}
                for event in accepted:
                    card = owners.get(event["cost_center_id"])
                    nodes = {address.partition("/")[0] for address in card["addresses"]
                             if "/" in address.partition(":")[0]} if card else set()
                    if nodes and node not in nodes:
                        raise HubError(403, "This cost center belongs to another node")
                receipts = self.ledger.record_many(accepted)
            return {"receipts": receipts, "recorded": sum(row["status"] == "recorded" for row in receipts),
                    "duplicates": sum(row["status"] == "duplicate" for row in receipts)}
        raise HubError(405, "This accounting operation does not support that method")


class Workers:
    """The phone queues work quickly; a durable background runner performs remote effects."""

    def __init__(self, manager, rates, hub=None):
        self.manager, self.rates = manager, rates
        # With a cloud spawner, a new teammate is a session a spawner starts, like one for a post nobody runs.
        self.hub = hub if hub is not None and manager is not None and manager.config.get("spawner") else None

    def rate_card(self, job):
        """New teammates start with an unknown all-in rate instead of a fictitious free rate."""
        name = job["spec"]["name"]
        card = make_card(name, name, aliases=[name], currency="USD", basis="allocated",
                         provenance="Not yet measured or allocated by the team",
                         metering_basis="Occupied work hour, including internal subagents")
        try:
            self.rates.bind_worker(card, job.get("address"))
        except RateError:
            log.error("Worker rate card could not be reconciled")

    def teammate(self, spec, ident):
        """Ask the hub for one more session in the spec's channel; it starts on the preferred connected spawner."""
        from .store import NODE
        spec = self.manager._spec(spec)
        if not isinstance(ident, str):
            raise ValueError("Choose a request ID")
        return self.hub.request_teammate(NODE, "user", ident, spec["topic"], f"New teammate @{spec['name']}: {spec['prompt']}")

    def browser(self, method, payload):
        if method == "GET":
            _fields(payload, ())
            return self.manager.list()
        action = payload.get("action")
        if action == "spawn":
            _fields(payload, ("action", "spec", "id"))
            if self.hub is not None:
                return self.teammate(payload["spec"], payload["id"])
            job = self.manager.submit(payload["spec"], payload["id"])
            self.rate_card(job)
            return job
        if action == "reconcile":
            _fields(payload, ("action", "id", "box_id"))
            if not isinstance(payload["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", payload["id"]):
                raise ValueError("Choose a tracked worker ID")
            try:
                return self.manager.reconcile(payload["id"], payload["box_id"])
            except KeyError:
                raise HubError(404, "No such tracked worker") from None
        _fields(payload, ("action", "id"))
        if action not in ("stop", "recheck", "stop_pool", "resume_pool"):
            raise ValueError("Choose spawn, stop, recheck, stop_pool or resume_pool")
        if not isinstance(payload["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", payload["id"]):
            raise ValueError("Choose a tracked worker or pool ID")
        try:
            return getattr(self.manager, action)(payload["id"])
        except KeyError:
            raise HubError(404, "No such tracked worker or pool") from None


# Every node's bridge syncs its conversations here, under the same origin and node tokens as the hub.
ARCHIVE_PATH = "/archive"


def conversation_archive(directory, hub):
    """The archive API, or None when its storage dependencies are not installed."""
    try:
        from archive.server import create_app
        from archive.store import SQLiteStore
    except ImportError as error:
        log.warning("Conversation archive disabled: %s", error)
        return None
    root = Path(directory) / "archive"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def node_for(request):
        try:
            return hub.authenticated_node(request)
        except HubError:
            return None

    return create_app(SQLiteStore(f"sqlite:///{root / 'archive.db'}"), node_for=node_for)


# Who the hub starts in the cloud for a request no running session can take.
# How often due routines are looked for; a routine fires within this many seconds of its minute.
ROUTINE_STEP = 15
STAFF_HARNESS = "claude"
STAFF_PROFILE = "default"


def staff_from_cloud(hub, workers) -> list[dict]:
    """Submit one cloud worker per request the hub could not staff on a machine; the jobs submitted.

    The job id comes from the task and its version, so a retry after a lost answer finds the
    same job instead of paying for a second worker.
    """
    submitted = []
    for due in hub.staffing_due():
        if workers is None:
            hub.staffing_mark(due["task"], due["version"], "unavailable", "cloud workers are not configured")
            continue
        digest = hashlib.sha256(f"{due['task']}@{due['version']}".encode()).hexdigest()
        spec = {"name": f"req-{digest[:8]}", "harness": STAFF_HARNESS, "model": None,
                "topic": due["topic"] or "general", "profile": STAFF_PROFILE, "prompt": due["prompt"]}
        try:
            job = workers.manager.submit(spec, f"staff-{digest[:24]}")
        except ValueError as error:
            hub.staffing_mark(due["task"], due["version"], "unavailable", str(error))
            continue
        workers.rate_card(job)
        hub.staffing_mark(due["task"], due["version"], "submitted", f"cloud worker job {job['id']}")
        submitted.append(job)
    return submitted


# A stopped cloud spawner is resumed at most this often, so a box that cannot start is not hammered.
SPAWNER_RETRY_SECONDS = 300
SPAWNER_STEP = 30
SPAWNER_UNIT = "one-conv-bridge.service"
# The cloud box sleeps after this long without a busy session: a later post starts a new session that takes over.
SPAWNER_IDLE_SECONDS = 30 * 60
# Boat keeps a stopped box's data 14 days; one that slept a week is woken so a fresh snapshot keeps it.
SPAWNER_SNAPSHOT_SECONDS = 7 * 86400
SPAWNER_CHECK_SECONDS = 3600


def manage_spawner(hub, boat, spawner: dict, state: dict) -> str:
    """Run the cloud spawner's box only while there is work for it.

    A post that no connected machine can take wakes the box; the box sleeps once no session on it has
    been busy for SPAWNER_IDLE_SECONDS. Sleeping loses nothing pushed: every session is in the hub's
    archive, and the session started for the next post reads it and takes over.
    """
    node, box, now = spawner["node"], spawner["box"], time.time()
    seen = hub.node_seen(node)
    online = seen is not None and 0 <= now - seen <= LEASE_SECONDS
    activity = hub.spawner_activity(node)
    if activity["wanted"]:
        hub.resume_spawner(node)
        state["active"] = now
        if online:
            return "online"
        return _start_spawner(boat, spawner, state, now, "a request waits for it")
    if online:
        if activity["busy"] or "active" not in state:
            state["active"] = now
            return "online"
        if now - state["active"] < SPAWNER_IDLE_SECONDS or not hub.pause_spawner(node):
            return "online"
        boat.stop_pool(box)
        state.pop("active", None)
        log.warning("cloud spawner %s had no busy session for %d minutes; stopped box %s",
                    node, SPAWNER_IDLE_SECONDS // 60, box)
        return "stopped"
    if now - state.get("checked", 0.0) < SPAWNER_CHECK_SECONDS:
        return "asleep"
    state["checked"] = now
    from .workers import machine_status
    row = boat.info(box)
    snapshot = row.get("snapshotCompletedAt") or ""
    if machine_status(row.get("state")) == "stopped" and snapshot and _age(snapshot, now) > SPAWNER_SNAPSHOT_SECONDS:
        hub.resume_spawner(node)
        return _start_spawner(boat, spawner, state, now, "its snapshot is a week old")
    return "asleep"


# Boat writes the box's agent credentials here at boot; the spawner reads it at every start.
BOAT_SECRETS_FILE = "/run/ascii-secrets/env.sh"
SPAWNER_DEFAULTS = {"profile": "default", "harness": "claude", "args": ["--dangerously-skip-permissions"],
                    "priority": 0, "env_file": BOAT_SECRETS_FILE}


SPAWNER_KEYS = {"node", "box", *SPAWNER_DEFAULTS}


def check_spawner(spawner, profiles) -> None:
    """Refuse a spawner block the controller could not provision, when it starts rather than at the box."""
    if (not isinstance(spawner, dict) or set(spawner) - SPAWNER_KEYS or not isinstance(spawner.get("node"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", spawner["node"])
            or spawner.get("box") is not None and not isinstance(spawner["box"], str)
            or spawner.get("profile", "default") not in profiles
            or spawner.get("harness", "claude") not in ("claude", "codex", "omp")
            or type(spawner.get("priority", 0)) is not int
            or not isinstance(spawner.get("args", []), list)):
        raise ValueError(f"the spawner block takes node and optionally {', '.join(sorted(SPAWNER_KEYS - {'node'}))}")


def _revision() -> str:
    """The commit this controller runs; a new one re-provisions the spawner box with it."""
    import subprocess
    result = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip()


def _spawner_token(manager, node: str) -> str:
    """The node's hub token: the one a worker pool on that node already holds, else a new one kept privately."""
    import json

    from .workers import _read, private_json
    pool = next((pool for pool in _read(manager.state_path)["pools"].values() if pool.get("node") == node), None)
    path = manager.directory / "tokens" / (f"{pool['id']}.json" if pool else f"spawner-{node}.json")
    if path.exists():
        return json.loads(path.read_text())["token"]
    token = manager.config["hub"].add_token(node)
    private_json(path, {"token": token})
    return token


def spawner_payload(manager, spawner: dict, git_user: dict) -> dict:
    """What the box helper needs to make the box a spawner node; the secrets stay out of the fingerprint."""
    settings = {**SPAWNER_DEFAULTS, **spawner}
    profile = manager.profiles[settings["profile"]]
    return {"operation": "spawner", "job_id": "spawner",
            "profile": {"repositories": profile["repositories"], "credential_refs": {}, "revisions": {}},
            "git_user": git_user,
            "spawner": {key: settings[key] for key in ("harness", "args", "priority", "env_file")},
            "hub": {"url": manager.config["public_hub_url"], "node": settings["node"]}}


def _fingerprint(payload: dict, revision: str) -> str:
    import json
    helper = (Path(__file__).with_name("worker_bootstrap.py")).read_bytes()
    return hashlib.sha256(json.dumps([payload, revision], sort_keys=True).encode() + helper).hexdigest()


def _drifted(hub, node: str, record: dict) -> bool:
    """Whether the box, publishing since it was provisioned, reports a checkout other than the provisioned one."""
    if not record.get("revision"):
        return False
    seen = hub.node_seen(node)
    if seen is None or time.time() - seen > LEASE_SECONDS or seen <= record.get("at", 0.0):
        return False
    revision, updated = hub.node_revision(node)
    # A bridge too old to report its checkout is itself a drift; a report from before provisioning is not news.
    return revision is None or (updated > record.get("at", 0.0) and revision != record["revision"])


def provision_spawner(manager, spawner: dict, wanted: bool) -> str:
    """Keep the spawner box built from code: create it when there is none, and run the helper's
    `spawner` operation on it whenever this controller's revision, the spawner settings, or the git
    identity the human's machine reports change.

    A sleeping box is provisioned when it next runs, and never while a request waits for it, since
    provisioning restarts its bridge.
    """
    import json

    from .workers import Gone, machine_status, private_json
    settings = {**SPAWNER_DEFAULTS, **spawner}
    record_path = manager.directory / "spawner.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    # The box this controller created or last provisioned outranks the configured one, which only seeds it.
    box = record.get("box") or settings.get("box")
    try:
        state = manager.boat.info(box).get("state") if box else None
    except Gone:
        log.warning("cloud spawner box %s no longer exists; creating another", box)
        box, state = None, None
    if box is None:
        box = manager.boat.create(manager.profiles[settings["profile"]], settings["node"], auto_stop=False)
        record = {"box": box}
        private_json(record_path, record)
        state = manager.boat.info(box).get("state")
        log.warning("created cloud spawner box %s for node %s", box, settings["node"])
    elif record.get("box") != box:
        record = {"box": box}
    spawner["box"] = box
    # The box commits as the human's own machine does; until one has reported its identity, nothing is set up.
    git_user = manager.config["hub"].git_identity(exclude=settings["node"])
    if git_user is None:
        return "waiting"
    payload = spawner_payload(manager, spawner, git_user)
    fingerprint = _fingerprint(payload, _revision())
    drifted = record.get("fingerprint") == fingerprint and _drifted(manager.config["hub"], settings["node"], record)
    if record.get("fingerprint") == fingerprint and not drifted:
        return "current"
    # A box whose checkout left what it was provisioned at serves no request well, so drift is fixed even then.
    if (wanted and not drifted) or time.time() - record.get("tried", 0.0) < SPAWNER_RETRY_SECONDS:
        return "waiting"
    if machine_status(state) != "available":
        return "asleep"
    record["tried"] = time.time()
    private_json(record_path, record)
    payload["service_account"] = manager.boat.credentials(manager.profiles[settings["profile"]])["service_account"]
    payload["hub"]["token"] = _spawner_token(manager, settings["node"])
    health = manager.boat.bootstrap(box, payload)
    if not isinstance(health, dict) or health.get("status") != "ready":
        stage = health.get("stage") if isinstance(health, dict) else None
        log.error("cloud spawner box %s was not provisioned: %s at %s", box,
                  health.get("status") if isinstance(health, dict) else "no receipt", stage)
        return "failed"
    record.update(fingerprint=fingerprint, revision=health.get("revision"), at=time.time())
    if drifted:
        log.warning("cloud spawner box %s had left its provisioned checkout; provisioned it again", box)
    # Only a failure delays the next attempt.
    record.pop("tried", None)
    private_json(record_path, record)
    log.warning("provisioned cloud spawner box %s at one-conv %s", box, str(health.get("revision", ""))[:12])
    return "provisioned"


def _age(stamp: str, now: float) -> float:
    return now - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _start_spawner(boat, spawner: dict, state: dict, now: float, why: str) -> str:
    """Resume a stopped box, or restart the bridge of a running one, at most once per SPAWNER_RETRY_SECONDS.

    A resumed box brings its bridge back by itself; restarting it then would kill the spawn it is starting.
    """
    if now - state.get("tried", 0.0) < SPAWNER_RETRY_SECONDS:
        return "waiting"
    state["tried"] = now
    from .workers import machine_status
    if machine_status(boat.info(spawner["box"]).get("state")) != "available":
        boat.resume_pool(spawner["box"])
        log.warning("cloud spawner %s started: %s; resumed box %s", spawner["node"], why, spawner["box"])
        return "resumed"
    boat._exec(spawner["box"], ["systemctl", "--user", "restart", SPAWNER_UNIT])
    log.warning("cloud spawner %s started: %s; restarted the bridge on box %s", spawner["node"], why, spawner["box"])
    return "restarted"


def application(directory, public_origin, *, worker_config=None, session_seconds=30 * 86400):
    """Keep browser cookies and agent bearer authorization on separate API routes."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    from .server import application as cloud_application
    from .server import bounded_json
    from .store import ControlStore

    accounting = Accounting(directory)
    providers = {"accounting": accounting.browser}
    store = ControlStore(Path(directory))
    workers = None
    if worker_config is not None:
        from .workers import WorkerManager
        config = {**worker_config, "public_hub_url": public_origin, "hub": store.hub}
        workers = Workers(WorkerManager(Path(directory) / "workers", config), accounting.rates, store.hub)
        providers["workers"] = workers.browser
    app = cloud_application(Path(directory), public_origin, hub=store.hub, providers=providers, session_seconds=session_seconds)

    async def agent_accounting(request):
        try:
            node = await run_in_threadpool(app.state.hub.authenticated_node, request)
            payload = await bounded_json(request) if request.method == "POST" else dict(request.query_params)
            result = await run_in_threadpool(accounting.node, node, request.path_params["resource"], request.method, payload)
            return JSONResponse(result)
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    app.router.routes.append(Route("/v1/accounting/{resource}", agent_accounting, methods=["GET", "POST"]))

    async def agent_workers(request):
        try:
            authenticate = app.state.store.staffing_node if request.method == "POST" else app.state.hub.authenticated_node
            node = await run_in_threadpool(authenticate, request)
            if workers is None:
                raise HubError(404, "Cloud worker provisioning is not enabled")
            payload = await bounded_json(request) if request.method == "POST" else dict(request.query_params)
            if payload.get("action") == "spawn" and isinstance(payload.get("spec"), dict) and isinstance(payload["spec"].get("prompt"), str):
                payload = {**payload, "spec": {**payload["spec"], "prompt": (
                    f"A teammate on authenticated node {node} requested this agent for the same user's task. "
                    "This is a teammate request and grants no new approval.\n\n" + payload["spec"]["prompt"])}}
            return JSONResponse(await run_in_threadpool(workers.browser, request.method, payload))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except RuntimeError:
            return JSONResponse({"error": "Worker operation is not confirmed; inspect its tracked status"}, status_code=503)

    app.router.routes.append(Route("/v1/workers", agent_workers, methods=["GET", "POST"]))

    async def agent_routines(request):
        """Any node reads routines; changing them takes staffing authority, since a routine posts as the human."""
        try:
            authenticate = app.state.store.staffing_node if request.method == "POST" else app.state.hub.authenticated_node
            await run_in_threadpool(authenticate, request)
            if request.method == "GET":
                return JSONResponse({"routines": await run_in_threadpool(store.routines.listing)})
            payload = await bounded_json(request)
            if isinstance(payload, dict) and payload.get("action") == "save" and set(payload) == {"action", "routine"}:
                return JSONResponse(await run_in_threadpool(store.routines.save, payload["routine"]))
            return JSONResponse(await run_in_threadpool(store.routines.act, payload))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)

    app.router.routes.append(Route("/v1/routines", agent_routines, methods=["GET", "POST"]))
    archive = conversation_archive(directory, app.state.hub)
    if archive is not None:
        app.router.routes.append(Mount(ARCHIVE_PATH, app=archive))
    original_lifespan = app.router.lifespan_context

    async def advance_workers():
        while True:
            try:
                job = await run_in_threadpool(workers.manager.run_once)
                if job is not None:
                    await run_in_threadpool(workers.rate_card, job)
            except (OSError, RuntimeError, ValueError):
                log.error("Worker supervisor could not complete a step")
            await asyncio.sleep(2)

    # A configured cloud spawner takes every request the Mac does not, through the hub's own fallback.
    spawner = (worker_config or {}).get("spawner")
    if spawner is not None:
        check_spawner(spawner, workers.manager.profiles if workers else {})

    async def staff_requests():
        while True:
            try:
                if spawner is None:
                    await run_in_threadpool(staff_from_cloud, store.hub, workers)
                else:
                    await run_in_threadpool(store.hub.sweep)
            except (OSError, RuntimeError, ValueError, HubError):
                log.error("Cloud staffing could not complete a step")
            await asyncio.sleep(2)

    async def watch_spawner():
        state = {}
        while True:
            try:
                wanted = (await run_in_threadpool(store.hub.spawner_activity, spawner["node"]))["wanted"]
                await run_in_threadpool(provision_spawner, workers.manager, spawner, wanted)
                await run_in_threadpool(manage_spawner, store.hub, workers.manager.boat, spawner, state)
            except (OSError, RuntimeError, ValueError, KeyError, HubError):
                log.error("Cloud spawner could not be brought back")
            await asyncio.sleep(SPAWNER_STEP)

    async def fire_routines():
        while True:
            try:
                await run_in_threadpool(store.routines.fire_due)
            except (OSError, RuntimeError, ValueError, HubError):
                log.error("Routines could not complete a step")
            await asyncio.sleep(ROUTINE_STEP)

    @asynccontextmanager
    async def lifespan(current):
        async with original_lifespan(current):
            loops = [asyncio.create_task(staff_requests()), asyncio.create_task(fire_routines())]
            if workers is not None:
                loops.append(asyncio.create_task(advance_workers()))
                if spawner is not None:
                    loops.append(asyncio.create_task(watch_spawner()))
            try:
                yield
            finally:
                for task in loops:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app.router.lifespan_context = lifespan
    app.state.accounting, app.state.workers, app.state.archive = accounting, workers, archive
    return app
