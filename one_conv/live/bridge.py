"""This machine's bridge to a live hub (`one-conv live serve`).

One bridge per machine keeps an outbound connection to the hub: it publishes the sessions
running here, posts what `live send` left in the outbox while the hub was unreachable, and
receives deliveries addressed to sessions here, handing each to that session's own adapter.
Deliveries arrive over the relay's WebSocket stream (see stream.py) when it serves one, and
by long-polling /v1/inbox otherwise (the Python relay, an ssh-forwarded hub). A dropped stream
is reopened with capped, jittered exponential backoff; the relay pushes again on connect every
delivery still without an outcome. Nothing listens on this machine.

Each delivery is claimed in a durable ingress log before its adapter runs, then acked with
its outcome. A delivery seen again (a lost ack) is acked with the recorded outcome and never
delivered twice. One claimed but without an outcome (the bridge stopped mid-delivery) is
reported `unconfirmed` and not retried, since the agent may already have it. Outcomes say
what the adapter did: `accepted` is not `read`. A delivery sent with when=idle to a busy
session is acked `held` and kept in held.py's store; a background thread hands held messages
to their sessions once idle, and the bridge then acks the relay with the final outcome.
A delivery of kind "guest" comes from a share link (`live share`): it is wrapped with the
guest trust rules, which tell the session the guest is not the user.

A delivery with mode `request` is for the bridge itself (a `live teleport` probe or spawn, see
provision.py): it is claimed the same way, acked `held`, run in its own thread, and answered
with its final outcome and result once that thread ends.

Beside delivery, the bridge syncs this machine's agent histories to the relay's archive, so
any machine can read a session here as `NODE/harness:id` and continue it.
"""

import json
import logging
import math
import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import held, hubclient, names, provision, router, tunnel
from .stream import InboxStream, StreamUnsupported
from .model import envelope, guest_envelope, live_home, parse_address

try:
    import fcntl
except ImportError:  # Windows: no flock, so a second bridge is not detected.
    fcntl = None

logger = logging.getLogger(__name__)

PUBLISH_SECONDS = 15.0
# Harness histories a node syncs to its hub's archive, so any session can read them by live address.
ARCHIVE_SOURCES = ("claude", "codex", "omp", "cursor", "cursor-cli", "agy")
# Cursor IDE chats are rows of one multi-gigabyte database: checking each costs a third of a
# second, against milliseconds for a file. They sync in their own slower lane, so the agent
# sessions another machine may need to take over are never queued behind them.
AGENT_SOURCES = tuple(source for source in ARCHIVE_SOURCES if source != "cursor")
IDE_SOURCES = ("cursor",)
IDE_SYNC_MINUTES = 15.0
# Short: a session on a machine that goes away can only be taken over up to its last sync.
ARCHIVE_SYNC_MINUTES = 1.0
ARCHIVE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "archive-sync.py"
# How long one inbox long-poll or stream read waits; also how long a stop request can take to be noticed.
POLL_SECONDS = 5.0
# How often held messages are checked against their sessions' status.
HELD_SECONDS = 1.0
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


def save_settings(hub: str, node: str, hub_port: int, token: str, **extra) -> None:
    """Save the connection; machine preferences already saved (accept_teleport, project roots) are kept."""
    _write_private(bridge_dir() / "serve.json", {**settings(), **extra, "hub": hub, "node": node,
                                                 "hub_port": hub_port, "token": token})


def save_accept_teleport(accept: bool) -> None:
    """Whether this machine starts sessions teleported from other nodes; read again for every request."""
    _write_private(bridge_dir() / "serve.json", {**settings(), provision.ACCEPT_KEY: accept})


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


def attachments_dir() -> Path:
    return live_home() / "attachments"


def with_attachments(text: str, attachments, fetch) -> str:
    """The message text followed by where each attached image was saved, so the session can read it.

    `fetch(attachment)` returns the saved file's path; an image the hub would not hand over is
    named as missing rather than dropped, so the receiver knows the message had one.
    """
    if not isinstance(attachments, list) or not attachments or fetch is None:
        return text
    lines = []
    for number, attachment in enumerate(attachments, 1):
        if not isinstance(attachment, dict) or not isinstance(attachment.get("id"), str):
            continue
        try:
            path = fetch(attachment)
        except (hubclient.HubRefused, hubclient.HubUnavailable, OSError, ValueError) as error:
            # A missing image must not lose the words it came with.
            lines.append(f"[Attached image {number} could not be fetched from the hub: {type(error).__name__}: {error}]")
            continue
        lines.append(f"[Attached image {number}: {path} ({attachment.get('media_type', 'image')}, "
                     f"{attachment.get('size', '?')} bytes). Read this file to view it.]")
    return text + ("\n\n" + "\n".join(lines) if lines else "")


def deliver_inbound(item: dict, directory: dict[str, dict] | None = None, fetch=None) -> tuple[str, str]:
    """Hand one relay delivery to the session here it is addressed to; returns (outcome, detail).

    Only a session listed live on this machine receives it: a stored-only Codex thread or a
    stopped one is refused rather than queued for nobody. An fyi is filed for `local feed`
    and never wakes the session. `fetch` saves an attached image locally and returns its path.
    A push with when=idle to a busy session is stored in held.py and reported `held`.
    """
    target, sender_route = str(item.get("to", "")), str(item.get("from", ""))
    harness, _ = parse_address(target)
    if harness is None:
        return "refused", f"{target!r} is not a session address"
    session = next((s for s in router.list_all(harness) if s.address == target), None)
    if session is None:
        return "refused", f"{target} is not live on this machine"
    if item.get("kind") == "guest":
        return _deliver_guest(item, session)
    remote = (directory or {}).get(sender_route, {})
    local = sender_route.partition("/")[2]
    # A sender that is not a session (the person at that node's terminal) has no return address.
    sender = RemoteSender(sender_route, remote.get("title") or "", remote.get("cwd") or "") if ":" in local else None
    mode, reply = item.get("mode", "push"), item.get("reply", "expected")
    text = with_attachments(item["text"], item.get("attachments"), fetch)
    if mode == "fyi":
        wrapped = envelope(text, sender, sender_route, "none", item.get("id"), remote.get("name"))
        router._append_fyi(target, sender_route, wrapped)
        return "accepted", "stored FYI"
    wrapped = envelope(text, sender, sender_route, reply, item.get("id"), remote.get("name"))
    try:
        with router._rate_slot(sender_route, target):
            if item.get("when") == "idle" and held.is_busy(session):
                held.hold(str(item.get("id")), target, sender_route, wrapped, delivery=item["delivery"])
                return "held", held.held_label(target)
            try:
                detail = router.ADAPTERS[harness].deliver(session, wrapped)
                # An adapter that cannot confirm a send says so in its answer rather than raising.
                return ("unconfirmed" if detail.startswith(UNKNOWN_RESULTS) else "accepted"), detail
            except Exception as error:
                raise _AdapterFailed(error) from error
    except _AdapterFailed as failure:
        # The adapter may have acted before failing, so the message is not known to be undelivered.
        return "unconfirmed", f"{type(failure.__cause__).__name__}: {failure.__cause__}"
    except router.LiveError as error:
        return "refused", str(error)


def _deliver_guest(item: dict, session) -> tuple[str, str]:
    """Hand a guest's message to the shared session, wrapped with the guest trust rules.

    The relay rate-limits guests per share, so the agent-to-agent rate slot does not apply.
    """
    try:
        wrapped = guest_envelope(str(item.get("text", "")), str(item.get("from", "")),
                                 str(item.get("label") or ""), item.get("id"))
    except ValueError as error:
        return "refused", str(error)
    try:
        detail = router.ADAPTERS[session.harness].deliver(session, wrapped)
    except Exception as error:
        # The adapter may have acted before failing, so the message is not known to be undelivered.
        return "unconfirmed", f"{type(error).__name__}: {error}"
    return ("unconfirmed" if detail.startswith(UNKNOWN_RESULTS) else "accepted"), detail


def publish_sessions(client: hubclient.HubClient) -> tuple[int, list[str]]:
    """Replace this node's entries in the relay directory with the sessions running here; (count, notes).

    The bridge does this on a timer; `live send` does it too when the relay does not know a
    session that just started here yet.
    """
    rows = [{"address": s.address, "name": names.name_for(s.address), "title": s.title, "cwd": s.cwd,
             "status": s.status, "product": s.product} for s in router.list_all()]
    try:
        return client.request("PUT", "/v1/sessions", {"sessions": rows})["published"], []
    except hubclient.HubRefused as error:
        if error.status != 409:
            raise
        # A name taken on another node must not keep every session here unreachable.
        count = client.request("PUT", "/v1/sessions", {"sessions": [{**r, "name": None} for r in rows]})["published"]
        return count, [f"names not published: {error}"]


class Requests:
    """Runs relay requests (teleport probes and spawns) in threads, so a spawn's wait never stalls deliveries."""

    def __init__(self, node: str, run=None):
        self.run = run or (lambda item: provision.handle(item, node, settings()))
        self.lock = threading.Lock()
        self.running: set[str] = set()
        self.finished: list[tuple[str, str, dict]] = []

    def start(self, item: dict) -> None:
        with self.lock:
            self.running.add(item["delivery"])
        threading.Thread(target=self._work, args=(item,), name=f"request-{item['delivery'][:8]}", daemon=True).start()

    def _work(self, item: dict) -> None:
        try:
            outcome, result = self.run(item)
        except Exception as error:  # A request must always be answered, whatever failed.
            logger.exception("request %s failed", item["delivery"])
            outcome, result = "refused", {"error": f"{type(error).__name__}: {error}"}
        with self.lock:
            self.running.discard(item["delivery"])
            self.finished.append((item["delivery"], outcome, result))

    def is_running(self, delivery: str) -> bool:
        with self.lock:
            return delivery in self.running

    def take(self) -> list[tuple[str, str, dict]]:
        with self.lock:
            rows, self.finished = self.finished, []
        return rows

    def put_back(self, rows: list[tuple[str, str, dict]]) -> None:
        with self.lock:
            self.finished = rows + self.finished


class Bridge:
    def __init__(self, client: hubclient.HubClient, node: str, ingress: Ingress, stream: InboxStream | None = None,
                 requests: Requests | None = None):
        self.client, self.node, self.ingress = client, node, ingress
        # Without a stream, deliveries come from long-polling /v1/inbox.
        self.stream = stream
        self.requests = requests or Requests(node)
        self.directory: dict[str, dict] = {}
        self.published_at = 0.0
        self.notes: list[str] = []

    def publish(self) -> int:
        """Replace this node's entries in the relay directory, renewing its lease."""
        count, self.notes = publish_sessions(self.client)
        self.directory = {row["address"]: row for row in self.client.request("GET", "/v1/sessions")["sessions"]}
        self.published_at = time.monotonic()
        return count

    def drain_outbox(self) -> int:
        """Post what live send queued, under hubclient.drain's policy; returns how many left the outbox."""
        result = hubclient.drain(self.client)
        for row in result["refused"]:
            logger.warning("the hub refused queued message %s: %s", row["id"], row["error"])
        return len(result["sent"]) + len(result["refused"])

    def fetch_attachment(self, attachment: dict) -> Path:
        """Save an attached image under this machine's live directory, once, and return its path."""
        from .hub import ATTACHMENT_ID, ATTACHMENT_TYPES
        ident = attachment["id"]
        if not ATTACHMENT_ID.fullmatch(ident):
            raise ValueError(f"{ident!r} is not an attachment id")
        suffix = ATTACHMENT_TYPES.get(str(attachment.get("media_type")), "bin")
        path = attachments_dir() / f"{ident}.{suffix}"
        if not path.exists():
            _, body = self.client.download(f"/v1/attachments/{ident}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
                handle.write(body)
        return path

    def handle_request(self, item: dict) -> str:
        """Run one relay request at most once: ack it `held`, answer with the final outcome once its thread ends.

        A request claimed by a bridge that stopped before answering is never run again; it is
        answered `unconfirmed`, since a spawn may already have started.
        """
        delivery = item["delivery"]
        fresh, recorded = self.ingress.claim(item)
        if fresh or (recorded is None and self.requests.is_running(delivery)):
            if fresh:
                logger.info("request %s from %s", delivery, item.get("from"))
                self.requests.start(item)
            self.ack_held(delivery)
            return "held"
        if recorded is None:
            outcome, result = "unconfirmed", {"error": "the bridge stopped while handling this request"}
            self.ingress.finish(delivery, outcome, json.dumps(result))
        else:
            outcome, detail = self.ingress.outcome(delivery)
            try:
                result = json.loads(detail or "{}")
            except ValueError:
                result = {}
        self.client.request("POST", "/v1/acks", {"delivery": delivery, "outcome": outcome, "result": result})
        return outcome

    def ack_finished_requests(self) -> int:
        """Answer each request whose thread ended; one the relay did not take is answered on the next step."""
        rows = self.requests.take()
        if any(outcome == "accepted" and result.get("address") for _, outcome, result in rows):
            # A session started for a teleport is published before its origin learns its address.
            self.publish()
        for index, (delivery, outcome, result) in enumerate(rows):
            self.ingress.finish(delivery, outcome, json.dumps(result))
            try:
                self.client.request("POST", "/v1/acks", {"delivery": delivery, "outcome": outcome, "result": result})
            except (hubclient.HubUnavailable, hubclient.HubRefused):
                self.requests.put_back(rows[index:])
                raise
        return len(rows)

    def handle(self, item: dict) -> str:
        """Deliver one inbox item at most once, then ack its outcome."""
        if item.get("mode") == "request":
            return self.handle_request(item)
        fresh, recorded = self.ingress.claim(item)
        if fresh:
            outcome, detail = deliver_inbound(item, self.directory, fetch=self.fetch_attachment)
            self.ingress.finish(item["delivery"], outcome, detail)
        elif recorded is None:
            outcome = "unconfirmed"
            self.ingress.finish(item["delivery"], outcome, "the bridge stopped while delivering it")
        else:
            outcome = recorded
        if outcome == "held":
            self.ack_held(item["delivery"])
        else:
            self.client.request("POST", "/v1/acks", {"delivery": item["delivery"], "outcome": outcome})
        return outcome

    def ack_held(self, delivery: str) -> None:
        """Tell the relay a delivery is held; a relay without the `held` outcome hands it out again, harmlessly."""
        try:
            self.client.request("POST", "/v1/acks", {"delivery": delivery, "outcome": "held"})
        except hubclient.HubRefused as error:
            if error.status != 400:
                raise
            logger.info("the relay does not record held deliveries: %s", error)

    def ack_finished_held(self) -> int:
        """Ack the relay with the final outcome of every held delivery that left the held store."""
        rows = held.unacked()
        for row in rows:
            outcome = held.RELAY_OUTCOMES[row["status"]]
            self.client.request("POST", "/v1/acks", {"delivery": row["delivery"], "outcome": outcome})
            self.ingress.finish(row["delivery"], outcome, row["detail"] or "")
            held.mark_acked(row["delivery"])
        return len(rows)

    def step(self, wait: float | None = None) -> None:
        wait = POLL_SECONDS if wait is None else wait
        if time.monotonic() - self.published_at >= PUBLISH_SECONDS:
            self.publish()
        self.drain_outbox()
        self.ack_finished_held()
        self.ack_finished_requests()
        if self.stream is not None:
            items = self.stream.receive(wait)
            for item in items:
                logger.info("delivery %s for %s arrived over the stream", item["delivery"], item.get("to"))
        else:
            items = self.client.request("GET", f"/v1/inbox?wait={wait:g}").get("messages", [])
        for item in items:
            self.handle(item)


def archive_sync_minutes() -> float:
    """ONE_CONV_ARCHIVE_SYNC_MINUTES, shared with the CLI's own sync; 0 turns the bridge's sync off."""
    try:
        minutes = float(os.environ.get("ONE_CONV_ARCHIVE_SYNC_MINUTES", ARCHIVE_SYNC_MINUTES))
    except ValueError:
        return ARCHIVE_SYNC_MINUTES
    return minutes if math.isfinite(minutes) else ARCHIVE_SYNC_MINUTES


class ArchiveSync:
    """Runs the archive sync for this node's histories in the background, one run at a time.

    Captures go to the hub's own archive with the node token and are uploaded under the node's
    name, so `NODE/harness:id` names them on every machine. The sync script skips unchanged
    conversations, so frequent runs cost a listing and a stat per conversation.
    """

    def __init__(self, minutes: float | None = None, script: Path = ARCHIVE_SCRIPT,
                 sources: tuple[str, ...] = ARCHIVE_SOURCES, lane: str = ""):
        self.seconds = (archive_sync_minutes() if minutes is None else minutes) * 60
        self.script, self.process, self.started = script, None, None
        self.sources, self.lane = sources, lane

    def poll(self, url: str, token: str, node: str) -> bool:
        """Start a sync when none runs and the last one started at least the interval ago."""
        if self.seconds <= 0 or not self.script.is_file():
            return False
        if self.process is not None and self.process.poll() is None:
            return False
        if self.started is not None and time.monotonic() - self.started < self.seconds:
            return False
        from one_conv.archive import HUB_ARCHIVE_PATH
        # The token travels in the environment, never in argv where other users could read it.
        env = {**os.environ, "ONE_CONV_ARCHIVE_URL": url + HUB_ARCHIVE_PATH, "ONE_CONV_ARCHIVE_TOKEN": token,
               "ONE_CONV_ARCHIVE_ORIGIN": node}
        sources = [argument for source in self.sources for argument in ("--source", source)]
        lane = ["--lane", self.lane] if self.lane else []
        log_path = bridge_dir() / f"archive-sync{'-' + self.lane if self.lane else ''}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            self.process = subprocess.Popen([sys.executable, str(self.script), *sources, *lane], env=env,
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        except OSError as error:
            logger.warning("archive sync could not start: %s", error)
            return False
        finally:
            os.close(output)
        self.started = time.monotonic()
        return True

    def stop(self) -> None:
        """End a running sync; an interrupted upload is never acknowledged, so the next run redoes it."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


class ArchiveLanes:
    """The agent-session lane every interval and the Cursor IDE lane every IDE_SYNC_MINUTES, side by side."""

    def __init__(self, minutes: float | None = None, script: Path = ARCHIVE_SCRIPT):
        agents = ArchiveSync(minutes, script, AGENT_SOURCES)
        ide = ArchiveSync(minutes, script, IDE_SOURCES, lane="ide")
        if ide.seconds > 0:
            ide.seconds = max(ide.seconds, IDE_SYNC_MINUTES * 60)
        self.lanes = (agents, ide)

    def poll(self, url: str, token: str, node: str) -> bool:
        return any([lane.poll(url, token, node) for lane in self.lanes])

    def stop(self) -> None:
        for lane in self.lanes:
            lane.stop()


class HeldDelivery:
    """Delivers held messages every HELD_SECONDS in a thread, whether or not the hub is reachable."""

    def __init__(self, seconds: float = HELD_SECONDS):
        self.seconds = seconds
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="held-delivery", daemon=True)

    def _run(self) -> None:
        while not self.stopped.wait(self.seconds):
            try:
                held.deliver_due(router.list_all, router.ADAPTERS)
            except (OSError, sqlite3.Error) as error:
                # A transient store or listing failure must not end delivery for the bridge's lifetime.
                logger.warning("held messages: %s", error)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)


def _report(**state) -> None:
    _write_private(bridge_dir() / "state.json", {**_read(bridge_dir() / "state.json"), **state, "pid": os.getpid()})


def serve(hub: str, node: str, token: str, hub_port: int = 8789, stopping=None,
          archive: "ArchiveSync | ArchiveLanes | None" = None) -> int:
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
    stream = None
    archive = archive or ArchiveLanes()
    from . import box
    keeper = box.Keeper()
    holding = HeldDelivery()
    holding.start()
    _report(hub=hub, node=node, started=time.time(), last_error=None)
    try:
        while not (stop_requested or stopping()):
            try:
                url = forward.open() if forward else hub
                # Every request checks the forward is still this ssh's, as the CLI's do.
                client = hubclient.HubClient(url, token, node, timeout=POLL_SECONDS + 15,
                                             listener_pid=forward.process.pid if forward else None)
                answered = client.request("GET", "/v1/whoami").get("node")
                if answered != node:
                    logger.error("this token belongs to node %r, not %r", answered, node)
                    return 1
                # The CLI posts through the same URL; while the bridge is down it fails and queues.
                _write_private(bridge_dir() / "hub.json", {"url": url, "node": node, "token": token,
                                                           **({"tunnel_pid": forward.process.pid} if forward else {})})
                try:
                    stream = InboxStream.open(client)
                    logger.info("receiving deliveries over the relay's stream")
                except StreamUnsupported as unsupported:
                    logger.info("%s; long-polling /v1/inbox", unsupported)
                bridge = Bridge(client, node, ingress, stream)
                _report(url=url, connected=time.time(), last_error=None, transport="stream" if stream else "long-poll")
                while not (stop_requested or stopping()):
                    if forward and not forward.alive():
                        raise tunnel.TunnelError("the ssh forward closed")
                    bridge.step()
                    # Reset only after a full step, so a stream that drops right after opening still backs off.
                    backoff = 1.0
                    archive.poll(url, token, node)
                    keeper.poll(client)
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
                if stream is not None:
                    stream.close()
                    stream = None
                if forward:
                    forward.close()
            # Jitter keeps bridges dropped together (a relay deploy) from redialling in lockstep.
            deadline = time.monotonic() + backoff * random.uniform(0.5, 1.0)
            while time.monotonic() < deadline and not (stop_requested or stopping()):
                time.sleep(0.2)
            backoff = min(backoff * 2, MAX_BACKOFF)
        return 0
    finally:
        holding.stop()
        archive.stop()
        _report(stopped=time.time())
        lock.close()
