"""List live sessions across harnesses, identify the sender, resolve a target and deliver."""

import json
import logging
import math
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import boat, claude, codex, cursor, hubclient, omp, team
from .model import LiveSession, envelope, is_session_id, live_home, parse_address

try:
    import fcntl
except ImportError:  # Windows has no flock; the rate log is then updated unlocked.
    fcntl = None

logger = logging.getLogger(__name__)
ADAPTERS = {"claude": claude, "codex": codex, "omp": omp, "cursor": cursor, "boat": boat}
# Listing Boat calls a remote API per sandbox, so it runs only when Boat is asked for by name.
ON_REQUEST = {"boat"}
# Two agents told to reply to each other would otherwise relay forever.
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 300
# Notices sent with --fyi stay readable by `local feed` this long.
FYI_KEEP_SECONDS = 7 * 24 * 3600
# messages.jsonl keeps this many of the latest messages, for `live web` and later reading.
MESSAGE_LOG = 2000
# Longer texts are cut in the log only; the delivered message is never shortened.
MESSAGE_LOG_TEXT = 20_000
# Clock skew tolerated on an existing notice; anything further ahead is corrupt.
FYI_MAX_SKEW_SECONDS = 3600


def _valid_fyi_time(at: object, now: float) -> bool:
    """A finite epoch time not far in the future; later notices are stamped no earlier than it."""
    # The range test comes first: it rejects NaN and infinities, and math.isfinite would
    # raise on an integer too large for a float.
    return (isinstance(at, (int, float)) and not isinstance(at, bool)
            and 0 < at <= now + FYI_MAX_SKEW_SECONDS and math.isfinite(at))


class LiveError(Exception):
    """A target that cannot be resolved or a delivery that must not happen."""


def list_all(harness: str | None = None) -> list[LiveSession]:
    sessions = []
    for name, adapter in ADAPTERS.items():
        if (harness and harness != name) or (not harness and name in ON_REQUEST):
            continue
        try:
            sessions += adapter.list_sessions()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            # One broken harness must not hide the others' sessions.
            logger.warning("%s: cannot list live sessions: %s", name, error)
    return sessions


def _process(pid: int) -> tuple[int, str] | None:
    """(parent pid, executable name) of `pid`, or None once the process is gone."""
    result = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], check=False, capture_output=True, text=True)
    parts = result.stdout.strip().split(None, 1)
    if len(parts) != 2:
        return None
    return int(parts[0]), Path(parts[1]).name


def whoami(explicit: str | None = None, meta: dict | None = None, pid: int | None = None) -> LiveSession | None:
    """The session this process acts for; None means the user at a plain terminal.

    Environment variables leak into grandchildren (an omp started from Claude Code keeps
    Claude's session id), so the nearest harness process in the ancestry decides.
    """
    if explicit:
        harness, ident = parse_address(explicit)
        if not harness:
            raise LiveError(f"--from must be an address like claude:<id>, got {explicit!r}")
        known = next((s for s in list_all(harness) if s.id == ident), None)
        if known is None:
            # Labels are self-reported; refusing addresses that are not running narrows invented senders.
            raise LiveError(f"--from {explicit} is not a running session (see `one-conv live ls`).")
        return known
    thread_id = (meta or {}).get("threadId")
    if thread_id:
        return next((s for s in codex.list_sessions() if s.id == thread_id), LiveSession("codex", thread_id))
    current = pid or os.getpid()
    for _ in range(32):
        found = claude.session_for_pid(current) or omp.session_for_pid(current)
        if found:
            return found
        step = _process(current)
        if step is None:
            return None
        parent, name = step
        if name == "codex":
            ident = os.environ.get("CODEX_THREAD_ID")
            return LiveSession("codex", ident) if ident else LiveSession("codex", "?unknown")
        if name == "cursor-agent":
            return LiveSession("cursor", cursor.chat_for_pid(current) or "?unknown")
        if name in ("Cursor", "Cursor Helper (Plugin)"):
            # The IDE hosts every chat in one process, so the calling chat cannot be told apart.
            return LiveSession("cursor", "?unknown")
        if parent <= 1:
            return None
        current = parent
    return None


def resolve(target: str, sessions: list[LiveSession] | None = None,
            exclude: LiveSession | None = None) -> LiveSession:
    """Match an address, id prefix, then title/cwd substring; `exclude` drops the sender first."""
    if target.startswith("@"):
        try:
            bound = team.address_for(target)
        except team.TeamError as error:
            raise LiveError(str(error)) from error
        if bound is None:
            raise LiveError(f"No agent is called {target}. Run `one-conv live names`.")
        target = bound
    harness, query = parse_address(target)
    if exclude is not None and target in (exclude.address, exclude.id):
        raise LiveError(f"{exclude.address} is the sending session itself.")
    pool = [s for s in (list_all(harness) if sessions is None else sessions)
            if harness in (None, s.harness) and (exclude is None or s.address != exclude.address)]
    for matches in ([s for s in pool if s.id == query],
                    [s for s in pool if s.id.startswith(query)],
                    [] if harness else [s for s in pool if query.lower() in f"{s.title} {s.cwd}".lower()]):
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            listing = "\n".join(f"  {s.address}  {s.title or s.cwd}" for s in matches)
            raise LiveError(f"{target!r} matches several live sessions:\n{listing}")
    if harness == "codex" and is_session_id(query):
        if codex.ended(query):
            # A message queued now would be recorded in the thread and answered by nobody.
            raise LiveError(f"codex:{query} was ended with `live stop`; resume it with `codex resume {query}` "
                            "before sending to it.")
        stored = codex.stored_session(query)
        if stored:
            return stored
    if harness == "cursor":
        if not is_session_id(query):
            raise LiveError(f"{query!r} is not a Cursor conversation id.")
        return LiveSession("cursor", query, status="not seen recently", delivery="stop hook",
                           endpoint=str(cursor.inbox(query)))
    hint = " A closed Claude Code session can be continued with `one-conv local send`." if harness == "claude" else ""
    raise LiveError(f"No live session matches {target!r}. Run `one-conv live ls`.{hint}")


@contextmanager
def _rate_slot(sender: str, target: str):
    """Hold the send log locked, refuse a pair over RATE_LIMIT, and record the send only if delivery succeeds."""
    log = live_home() / "sent.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with (live_home() / "sent.lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        try:
            rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            rows = []
        recent = [r for r in rows if now - r["at"] < RATE_WINDOW_SECONDS]
        if sum(1 for r in recent if r["from"] == sender and r["to"] == target) >= RATE_LIMIT:
            raise LiveError(f"{sender} already sent {RATE_LIMIT} messages to {target} in the last "
                            f"{RATE_WINDOW_SECONDS // 60} minutes; refusing so two agents cannot relay forever.")
        yield
        log.write_text("".join(json.dumps(r) + "\n" for r in [*recent, {"from": sender, "to": target, "at": now}]))


def peer_key(first: str, second: str) -> str:
    """`peers.json` key for an exchange: both addresses sorted, joined by one space."""
    return " ".join(sorted((first, second)))


def _record_peers(sender: str, target: str) -> None:
    """Note that two sessions exchanged a message; `local feed` follows recent partners from this."""
    path = live_home() / "peers.json"
    with (live_home() / "sent.lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            peers = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            # A corrupt file would otherwise block every later record; it only seeds feed defaults.
            peers = {}
        if not isinstance(peers, dict):
            peers = {}
        peers[peer_key(sender, target)] = time.time()
        partial = path.with_name(f".peers.{os.getpid()}.tmp")
        partial.write_text(json.dumps(peers, indent=1, sort_keys=True))
        os.replace(partial, path)


def fyi_inbox(address: str) -> Path:
    """Append-only JSONL of notices for `address`, read by `local feed`; only senders prune it."""
    harness, ident = parse_address(address)
    if harness is None or not is_session_id(ident):
        raise LiveError(f"{address!r} is not a session address.")
    return live_home() / "fyi" / f"{harness}-{ident}.jsonl"


def _append_fyi(target: str, sender: str, text: str) -> None:
    """Add one notice and drop those older than FYI_KEEP_SECONDS.

    It takes its own lock: the caller already holds `sent.lock`, and a second flock on
    that file from this process would wait on itself.
    """
    path = fyi_inbox(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".fyi.lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                # A corrupt line is dropped on rewrite; failing here would lose every later notice.
                continue
            # A row whose time is missing, infinite or far ahead would stamp every later
            # notice with it and never age out, so it is dropped like a corrupt line.
            if isinstance(row, dict) and _valid_fyi_time(row.get("at"), now):
                rows.append(row)
        # Never decreasing `at` lets a reader resume after the last entry it saw.
        at = max([now, *(r["at"] for r in rows)])
        kept = [r for r in rows if now - r["at"] < FYI_KEEP_SECONDS]
        kept.append({"id": uuid.uuid4().hex, "at": at, "from": sender, "to": target, "text": text})
        partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        partial.write_text("".join(json.dumps(r) + "\n" for r in kept))
        os.replace(partial, path)


def topic_of(target: str) -> str | None:
    """The topic a `#name` or `topic:name` target addresses, else None."""
    if target.startswith("#"):
        return target[1:]
    if target.startswith("topic:"):
        return target[len("topic:"):]
    return None


def log_message(row: dict) -> None:
    """Append one message to <live_home>/messages.jsonl, keeping the latest MESSAGE_LOG.

    The bridge calls this for messages it delivers here, so the log covers both directions.
    """
    path = live_home() / "messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {**row, "text": str(row.get("text", ""))[:MESSAGE_LOG_TEXT], "at": row.get("at") or time.time()}
    with (live_home() / ".messages.lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        kept = [line for line in lines if line.strip()][-(MESSAGE_LOG - 1):]
        partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        partial.write_text("".join(line + "\n" for line in kept) + json.dumps(entry) + "\n")
        os.replace(partial, path)


def _logged(receipt: dict, text: str, topic: str | None, fyi: bool, reply: str) -> dict:
    """Record a finished send; the message is already out, so a log failure only warns."""
    statuses = {row["to"]: row["status"] for row in receipt.get("deliveries", [])}
    try:
        log_message({"id": receipt.get("id"), "from": receipt.get("from"),
                     "to": f"#{receipt['topic']}" if "deliveries" in receipt else receipt.get("to"),
                     "topic": receipt.get("topic") or topic, "mode": "fyi" if fyi else "push",
                     "reply": "none" if fyi else reply,
                     "status": statuses or receipt.get("status"), "text": text})
    except OSError as error:
        logger.warning("sent, but could not log the message: %s", error)
    return receipt


def send(target: str, text: str, sender: LiveSession | None, origin: str = "unknown",
         fyi: bool = False, reply: str = "expected", topic: str | None = None,
         message_id: str | None = None) -> dict:
    """Deliver to one session, or to every member of a topic for a `#topic` target.

    `message_id` is chosen once and reused on retry, so a receiver can drop a duplicate.
    """
    message_id = message_id or uuid.uuid4().hex
    named = topic_of(target)
    try:
        if named is not None:
            return _logged(_send_topic(named, text, sender, origin, fyi, reply, message_id), text, None, fyi, reply)
        if topic is not None:
            topic = team._check("topic", topic)
    except team.TeamError as error:
        raise LiveError(str(error)) from error
    receipt = _send_one(target, text, sender, origin, fyi, reply, topic, message_id)
    if topic is not None:
        team.record(topic, message_id, receipt["from"], text)
    return _logged(receipt, text, topic, fyi, reply)


def _send_topic(topic: str, text: str, sender: LiveSession | None, origin: str, fyi: bool,
                reply: str, message_id: str) -> dict:
    """Record the message on the topic, then deliver it to each member but the sender."""
    topic = team._check("topic", topic)
    sender_label = sender.address if sender else origin
    try:
        hub = hubclient.configured()
    except hubclient.HubRefused as error:
        raise LiveError(str(error)) from error
    if hub is not None:
        return _post_topic(hub, topic, text, sender_label, "none" if fyi else reply, message_id,
                           "fyi" if fyi else "push")
    team.record(topic, message_id, sender_label, text)
    deliveries = []
    for member in team.members(topic):
        if member == sender_label:
            continue
        try:
            deliveries.append(_send_one(member, text, sender, origin, fyi, reply, topic, message_id))
        except (LiveError, RuntimeError, OSError, ValueError) as error:
            # A member that cannot be reached now still finds the message when it catches up.
            deliveries.append({"to": member, "status": f"not delivered: {error}"})
    return {"topic": topic, "id": message_id, "from": sender_label, "deliveries": deliveries}


def _post_topic(hub, topic: str, text: str, sender: str, reply: str, message_id: str,
                mode: str = "push") -> dict:
    """Post once; the hub records the message and fans it out to every member on every machine.

    The message sits in the bridge's outbox from before the request until the hub answers,
    so a crash or an unreachable hub leaves it for the bridge to repost under the same id.
    The sender joins the topic first, as sending to a local topic does, so replies reach it.
    """
    if ":" in sender:
        try:
            hub.join(topic, sender)
        except (hubclient.HubRefused, hubclient.HubUnavailable) as error:
            # The post itself reports a real problem; a failed join alone must not block it.
            logger.info("could not join #%s before posting: %s", topic, error)
    # mode is the delivery mode (wake the receiver or not), kept apart from the reply policy.
    message = {"id": message_id, "to": f"#{topic}", "from": sender, "topic": topic,
               "mode": mode, "reply": reply, "text": text}
    try:
        receipt = hubclient.submit(hub, message)
    except (hubclient.HubRefused, hubclient.PendingConflict) as error:
        raise LiveError(str(error)) from error
    if receipt is None:
        return {"topic": topic, "id": message_id, "from": sender, "via": "hub",
                "deliveries": [{"to": f"#{topic}", "status": "queued for the hub: no receipt yet, the bridge "
                                                             "will resend it under the same id"}]}
    return {"topic": topic, "id": message_id, "from": sender, "via": "hub",
            "deliveries": [{"to": row["to"], "status": row["status"]} for row in receipt.get("recipients", [])]}


def _send_one(target: str, text: str, sender: LiveSession | None, origin: str, fyi: bool,
              reply: str, topic: str | None, message_id: str) -> dict:
    session = resolve(target, exclude=sender)
    if sender is not None and session.address == sender.address:
        raise LiveError(f"{session.address} is the sending session itself.")
    sender_label = sender.address if sender else origin
    if fyi and session.harness == "boat":
        raise LiveError("--fyi cannot reach a Boat conversation: a sandbox has no inbox on this "
                        "machine's feed. Push it once the conversation is idle, or skip it.")
    if session.harness == "boat" and reply == "expected":
        reply = "inline"
    handle = team.name_for(sender.address) if sender is not None else None
    wrapped = envelope(text, sender, origin, "none" if fyi else reply, message_id, topic, handle)
    with _rate_slot(sender_label, session.address):
        if fyi:
            _append_fyi(session.address, sender_label, wrapped)
            status, delivery = "stored FYI", "fyi"
        else:
            status, delivery = ADAPTERS[session.harness].deliver(session, wrapped), session.delivery
    receipt = {"to": session.address, "title": session.title, "cwd": session.cwd,
               "delivery": delivery, "status": status, "from": sender_label, "id": message_id}
    # Only two identified sessions form a pair; "user", "unknown" and "?unknown" ids do not.
    if sender is not None and not sender.id.startswith("?"):
        try:
            _record_peers(sender.address, session.address)
        except (OSError, ValueError) as error:
            # The message is already delivered; a retry would duplicate it, so warn instead of failing.
            logger.warning("delivered, but could not record the peer pair: %s", error)
            receipt["warning"] = f"peer pair not recorded: {error}"
    return receipt
