"""Wire cloud messaging, durable worker jobs and shared accounting into one service."""

import asyncio
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
            return {"card": self.rates.register(payload["card"], replace=payload.get("replace", False))}
        raise ValueError("Choose quote or register")

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
        try:
            card = self.rates.show(name)
        except RateError:
            card = make_card(name, name, aliases=[name], currency="USD", basis="allocated",
                             provenance="Not yet measured or allocated by the team",
                             metering_basis="Occupied work hour, including internal subagents")
        card = {key: value for key, value in card.items() if key != "updated_at"}
        if job.get("address") and job["address"] not in card["addresses"]:
            card["addresses"] = sorted([*card["addresses"], job["address"]])
        try:
            self.rates.register(card, replace=True)
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


def application(directory, public_origin, *, worker_config=None, session_seconds=30 * 86400):
    """Keep browser cookies and agent bearer authorization on separate API routes."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from starlette.routing import Route

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
            node = await run_in_threadpool(app.state.hub.authenticated_node, request)
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

    @asynccontextmanager
    async def lifespan(current):
        async with original_lifespan(current):
            task = asyncio.create_task(advance_workers()) if workers is not None else None
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app.router.lifespan_context = lifespan
    app.state.accounting, app.state.workers = accounting, workers
    return app
