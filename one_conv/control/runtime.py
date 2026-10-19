"""Wire cloud messaging, durable worker jobs and shared accounting into one service."""

import asyncio
import hashlib
import logging
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from ..hr import RateError, RateStore, make_card, quote
from ..live.hub import HubError

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

    def __init__(self, manager, rates):
        self.manager, self.rates = manager, rates

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

    def browser(self, method, payload):
        if method == "GET":
            _fields(payload, ())
            return self.manager.list()
        action = payload.get("action")
        if action == "spawn":
            _fields(payload, ("action", "spec", "id"))
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
        workers = Workers(WorkerManager(Path(directory) / "workers", config), accounting.rates)
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

    async def staff_requests():
        while True:
            try:
                await run_in_threadpool(staff_from_cloud, store.hub, workers)
            except (OSError, RuntimeError, ValueError, HubError):
                log.error("Cloud staffing could not complete a step")
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(current):
        async with original_lifespan(current):
            loops = [asyncio.create_task(staff_requests())]
            if workers is not None:
                loops.append(asyncio.create_task(advance_workers()))
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
