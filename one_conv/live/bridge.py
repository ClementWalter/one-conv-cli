"""This machine's bridge to a live hub (`one-conv live serve`).

One bridge per machine keeps an outbound connection to the hub: it publishes the sessions
running here, posts what `live send` left in the outbox while the hub was unreachable, and
long-polls for deliveries addressed to sessions here, handing each to that session's own
adapter. Nothing listens on this machine.

Each delivery is claimed in a durable ingress log before its adapter runs, then acked with
its outcome. A delivery seen again (a lost ack) is acked with the recorded outcome and never
delivered twice. One claimed but without an outcome (the bridge stopped mid-delivery) is
reported `unconfirmed` and not retried, since the agent may already have it. Outcomes say
what the adapter did: `accepted` is not `read`.
"""

import json
import logging
import os
import signal
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import hubclient, router, team, tunnel
from .model import envelope, live_home, parse_address

try:
    import fcntl
except ImportError:  # Windows: no flock, so a second bridge is not detected.
    fcntl = None

logger = logging.getLogger(__name__)

PUBLISH_SECONDS = 15.0
# How long one inbox long-poll waits; also how long a stop request can take to be noticed.
POLL_SECONDS = 5.0
MAX_BACKOFF = 30.0


def bridge_dir() -> Path:
    return live_home() / "bridge"


def _write_private(path: Path, data: dict) -> None:
    """Replace a file atomically, readable only by this user: it may hold the node token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(partial, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def settings() -> dict:
    """What `live serve` was last started with: hub, node, hub_port and token."""
    return _read(bridge_dir() / "serve.json")


def save_settings(hub: str, node: str, hub_port: int, token: str) -> None:
    _write_private(bridge_dir() / "serve.json", {"hub": hub, "node": node, "hub_port": hub_port, "token": token})


def status() -> dict:
    """The running bridge's last reported state, with whether it is still running."""
    state = _read(bridge_dir() / "state.json")
    state["running"] = _held_by_other(bridge_dir() / "serve.lock")
    state["outbox"] = len(list(hubclient.outbox().glob("*.json"))) if hubclient.outbox().is_dir() else 0
    return state


def _held_by_other(path: Path) -> bool:
    if fcntl is None or not path.exists():
        return False
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def stop() -> bool:
    """Ask the running bridge to stop; False when none is running."""
    state = _read(bridge_dir() / "state.json")
    if not status()["running"] or not isinstance(state.get("pid"), int):
        return False
    os.kill(state["pid"], signal.SIGTERM)
    return True


class Ingress:
    """The durable record of every delivery this machine has taken from the hub."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, id TEXT, recipient TEXT,"
                       " sender TEXT, outcome TEXT, detail TEXT, claimed REAL NOT NULL, finished REAL)")

    def claim(self, item: dict) -> tuple[bool, str | None]:
        """(newly claimed, recorded outcome); committed before any adapter runs."""
        with closing(sqlite3.connect(self.path)) as db, db:
            inserted = db.execute("INSERT OR IGNORE INTO deliveries (delivery, id, recipient, sender, claimed)"
                                  " VALUES (?, ?, ?, ?, ?)", (item["delivery"], item.get("id"), item.get("to"),
                                                              item.get("from"), time.time())).rowcount
            if inserted:
                return True, None
            row = db.execute("SELECT outcome FROM deliveries WHERE delivery = ?", (item["delivery"],)).fetchone()
        return False, row[0]

    def finish(self, delivery: str, outcome: str, detail: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE deliveries SET outcome = ?, detail = ?, finished = ? WHERE delivery = ?",
                       (outcome, detail, time.time(), delivery))

    def outcome(self, delivery: str) -> tuple[str | None, str | None]:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT outcome, detail FROM deliveries WHERE delivery = ?", (delivery,)).fetchone()
        return (row[0], row[1]) if row else (None, None)


@dataclass(frozen=True)
class RemoteSender:
    """A sender on another machine, as the envelope names it: its full NODE/harness:id route."""

    address: str
    title: str = ""
    cwd: str = ""

    @property
    def id(self) -> str:
        return self.address


# Adapter results that say the outcome is not known yet; the sender must not read them as accepted.
UNKNOWN_RESULTS = ("pending", "unconfirmed")


class _AdapterFailed(Exception):
    pass


def deliver_inbound(item: dict, directory: dict[str, dict] | None = None) -> tuple[str, str]:
    """Hand one hub delivery to the session here it is addressed to; returns (outcome, detail).

    Only a session listed live on this machine receives it: a stored-only Codex thread or a
    stopped one is refused rather than queued for nobody. An fyi is filed for `local feed`
    and never wakes the session.
    """
    target, sender_route = str(item.get("to", "")), str(item.get("from", ""))
    harness, _ = parse_address(target)
    if harness is None:
        return "refused", f"{target!r} is not a session address"
    session = next((s for s in router.list_all(harness) if s.address == target), None)
    if session is None:
        return "refused", f"{target} is not live on this machine"
    remote = (directory or {}).get(sender_route, {})
    local = sender_route.partition("/")[2]
    # A sender that is not a session (the person at that node's terminal) has no return address.
    sender = RemoteSender(sender_route, remote.get("title") or "", remote.get("cwd") or "") if ":" in local else None
    mode, reply, topic = item.get("mode", "push"), item.get("reply", "expected"), item.get("topic")
    if mode == "fyi":
        if harness == "boat":
            return "refused", "an fyi cannot reach a Boat conversation: a sandbox has no feed on this machine"
        wrapped = envelope(item["text"], sender, sender_route, "none", item.get("id"), topic, remote.get("name"))
        router._append_fyi(target, sender_route, wrapped)
        return "accepted", "stored FYI"
    if harness == "boat" and reply == "expected":
        reply = "inline"
    wrapped = envelope(item["text"], sender, sender_route, reply, item.get("id"), topic, remote.get("name"))
    try:
        with router._rate_slot(sender_route, target):
            try:
                detail = router.ADAPTERS[harness].deliver(session, wrapped)
                # Boat reports a send it could not confirm as pending/unconfirmed rather than raising.
                return ("unconfirmed" if detail.startswith(UNKNOWN_RESULTS) else "accepted"), detail
            except Exception as error:
                raise _AdapterFailed(error) from error
    except _AdapterFailed as failure:
        # The adapter may have acted before failing, so the message is not known to be undelivered.
        return "unconfirmed", f"{type(failure.__cause__).__name__}: {failure.__cause__}"
    except router.LiveError as error:
        return "refused", str(error)


class Bridge:
    def __init__(self, client: hubclient.HubClient, node: str, ingress: Ingress):
        self.client, self.node, self.ingress = client, node, ingress
        self.directory: dict[str, dict] = {}
        self.published_at = 0.0
        self.notes: list[str] = []

    def publish(self) -> int:
        """Replace this node's entries in the hub directory, renewing its lease."""
        rows = [{"address": s.address, "name": team.name_for(s.address), "title": s.title, "cwd": s.cwd,
                 "status": s.status} for s in router.list_all()]
        try:
            count = self.client.request("PUT", "/v1/sessions", {"sessions": rows})["published"]
            self.notes = []
        except hubclient.HubRefused as error:
            if error.status != 409:
                raise
            # A name taken on another node must not keep every session here unreachable.
            self.notes = [f"names not published: {error}"]
            count = self.client.request("PUT", "/v1/sessions", {"sessions": [{**r, "name": None} for r in rows]})[
                "published"]
        self.directory = {row["address"]: row for row in self.client.request("GET", "/v1/sessions")["sessions"]}
        self.published_at = time.monotonic()
        return count

    def drain_outbox(self) -> int:
        """Post messages queued while the hub was unreachable, oldest first, under their own ids."""
        sent = 0
        paths = sorted(hubclient.outbox().glob("*.json"), key=lambda p: p.stat().st_mtime) \
            if hubclient.outbox().is_dir() else []
        for path in paths:
            try:
                message = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            try:
                hubclient.post_queued(self.client, message, path)
            except hubclient.HubRefused as error:
                # Only a terminal refusal reaches here; it is set aside for a person to look at.
                refused = hubclient.outbox() / "refused"
                refused.mkdir(exist_ok=True)
                _write_private(refused / path.name, {**message, "error": str(error)})
                path.unlink(missing_ok=True)
                logger.warning("the hub refused queued message %s: %s", path.stem, error)
            sent += 1
        return sent

    def handle(self, item: dict) -> str:
        """Deliver one inbox item at most once, then ack its outcome."""
        fresh, recorded = self.ingress.claim(item)
        if fresh:
            outcome, detail = deliver_inbound(item, self.directory)
            self.ingress.finish(item["delivery"], outcome, detail)
        elif recorded is None:
            outcome = "unconfirmed"
            self.ingress.finish(item["delivery"], outcome, "the bridge stopped while delivering it")
        else:
            outcome = recorded
        self.client.request("POST", "/v1/acks", {"delivery": item["delivery"], "outcome": outcome})
        return outcome

    def step(self, wait: float | None = None) -> None:
        wait = POLL_SECONDS if wait is None else wait
        if time.monotonic() - self.published_at >= PUBLISH_SECONDS:
            self.publish()
        self.drain_outbox()
        for item in self.client.request("GET", f"/v1/inbox?wait={wait:g}").get("messages", []):
            self.handle(item)


def _report(**state) -> None:
    _write_private(bridge_dir() / "state.json", {**_read(bridge_dir() / "state.json"), **state, "pid": os.getpid()})


def serve(hub: str, node: str, token: str, hub_port: int = 8789, stopping=None) -> int:
    """Run the bridge until SIGTERM/SIGINT or `stopping()`; returns the exit status."""
    lock_path = bridge_dir() / "serve.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a")
    if fcntl is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.error("a bridge is already running on this machine; see `one-conv live serve --status`")
            return 1
    stop_requested = []
    if threading.current_thread() is threading.main_thread():  # Python allows signal handlers only there.
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop_requested.append(True))
    stopping = stopping or (lambda: False)
    forward = tunnel.Tunnel(tunnel.destination(hub), hub_port) if hub.startswith("ssh://") else None
    ingress, backoff = Ingress(bridge_dir() / "ingress.db"), 1.0
    _report(hub=hub, node=node, started=time.time(), last_error=None)
    try:
        while not (stop_requested or stopping()):
            try:
                url = forward.open() if forward else hub
                client = hubclient.HubClient(url, token, node, timeout=POLL_SECONDS + 15)
                answered = client.request("GET", "/v1/whoami").get("node")
                if answered != node:
                    logger.error("this token belongs to node %r, not %r", answered, node)
                    return 1
                # The CLI posts through the same URL; while the bridge is down it fails and queues.
                _write_private(bridge_dir() / "hub.json", {"url": url, "node": node, "token": token,
                                                           **({"tunnel_pid": forward.process.pid} if forward else {})})
                bridge = Bridge(client, node, ingress)
                _report(url=url, connected=time.time(), last_error=None)
                backoff = 1.0
                while not (stop_requested or stopping()):
                    if forward and not forward.alive():
                        raise tunnel.TunnelError("the ssh forward closed")
                    bridge.step()
                    _report(last_ok=time.time(), published=len(bridge.directory), notes=bridge.notes)
            except (hubclient.HubUnavailable, tunnel.TunnelError) as error:
                logger.warning("hub unreachable, retrying in %.0fs: %s", backoff, error)
                _report(last_error=str(error), last_error_at=time.time())
            except hubclient.HubRefused as error:
                if error.status == 401:
                    logger.error("the hub rejected this node's token: %s", error)
                    _report(last_error=str(error), last_error_at=time.time())
                    return 1
                logger.warning("the hub refused a request, retrying in %.0fs: %s", backoff, error)
                _report(last_error=str(error), last_error_at=time.time())
            finally:
                if forward:
                    forward.close()
            deadline = time.monotonic() + backoff
            while time.monotonic() < deadline and not (stop_requested or stopping()):
                time.sleep(0.2)
            backoff = min(backoff * 2, MAX_BACKOFF)
        return 0
    finally:
        _report(stopped=time.time())
        lock.close()
