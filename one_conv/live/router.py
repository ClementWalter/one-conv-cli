"""List live sessions across harnesses, identify the sender, resolve a target and deliver."""

import json
import logging
import math
import os
import re
import subprocess
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from . import agy, claude, codex, cursor, held, hubclient, names, omp, share
from .model import GUEST_ADDRESS, LiveSession, envelope, is_session_id, live_home, parse_address

try:
    import fcntl
except ImportError:  # Windows has no flock; the rate log is then updated unlocked.
    fcntl = None

logger = logging.getLogger(__name__)
ADAPTERS = {"claude": claude, "codex": codex, "omp": omp, "cursor": cursor, "agy": agy}
# Two agents told to reply to each other would otherwise relay forever.
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 300
# Notices sent with --fyi stay readable by `local feed` this long.
FYI_KEEP_SECONDS = 7 * 24 * 3600
# Clock skew tolerated on an existing notice; anything further ahead is corrupt.
FYI_MAX_SKEW_SECONDS = 3600
# The person every agent works for: a message to it is a note in their inbox, never a new turn.
USER = "user"
# The prefix of a share's guest address (`live share`): answers go to its page on the relay.
GUEST = "guest:"
TAG = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
TAGS_PER_NOTE = 8
NOTES_LIMIT = 200


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
        if harness and harness != name:
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
    # The Cursor CLI runs as its `agent` symlink; whoami knows it by the binary's own name.
    return int(parts[0]), "cursor-agent" if cursor.is_cursor_agent(parts[1]) else Path(parts[1]).name


def node_name() -> str | None:
    """This machine's name on the live hub, or None when none is configured."""
    name = os.environ.get("ONE_CONV_HUB_NODE")
    if not name:
        try:
            name = json.loads((live_home() / "bridge" / "hub.json").read_text()).get("node")
        except (OSError, ValueError, AttributeError):
            return None
    return name if isinstance(name, str) and NODE_NAME.fullmatch(name) else None


def origin(interactive: bool, pid: int | None = None) -> str:
    """The sender label when no agent session is calling: a human at a terminal, Claude Desktop, or unknown.

    Claude Desktop runs extension servers as its own children, so its chats and Cowork
    sessions are told apart from other callers but not from each other.
    """
    if interactive:
        return "user"
    current = pid or os.getpid()
    for _ in range(32):
        step = _process(current)
        if step is None:
            break
        parent, name = step
        if name == "Claude" or name.startswith("Claude Helper"):
            node = node_name()
            return f"{node}/claude-desktop" if node else "claude-desktop"
        if parent <= 1:
            break
        current = parent
    return "unknown"


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
        found = claude.session_for_pid(current) or omp.session_for_pid(current) or agy.session_for_pid(current)
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
        if name == "agy":
            # Without the hooks installed, agy's shell commands still carry their conversation id;
            # its MCP servers get none.
            ident = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
            return LiveSession("agy", ident if ident and is_session_id(ident) else "?unknown")
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
            bound = names.address_for(target)
        except names.NameRefused as error:
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
        stored = codex.stored_session(query)
        if stored:
            return stored
    if harness == "cursor":
        if not is_session_id(query):
            raise LiveError(f"{query!r} is not a Cursor conversation id.")
        return LiveSession("cursor", query, status="not seen recently", delivery="stop hook",
                           endpoint=str(cursor.inbox(query)))
    if harness == "agy" and is_session_id(query) and (agy.agy_home() / "brain" / query).is_dir():
        # A closed conversation's hooks hand the message over once `agy --conversation` resumes it.
        return LiveSession("agy", query, status="not running", delivery="lifecycle hook", endpoint=str(agy.inbox(query)))
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


def _hub():
    try:
        return hubclient.configured()
    except hubclient.HubRefused as error:
        raise LiveError(str(error)) from error


def send(target: str, text: str, sender: LiveSession | None, origin: str = "unknown",
         fyi: bool = False, reply: str = "expected", message_id: str | None = None, images=(),
         tags=(), when: str = "now", wait_reply: bool = True) -> dict:
    """Deliver to one session: here, in a cloud conversation, or on another machine through the relay.

    A share link (`https://<relay>/s/<id>#<secret>`, from `live share`) reaches the session
    someone shared with you: the answer comes back in the receipt's `reply` unless `wait_reply`
    is false.

    `message_id` is chosen once and reused on retry, so a receiver can drop a duplicate.
    `when="now"` hands the message over at once, so a busy agent takes it at its next tool
    boundary; `when="idle"` holds it until the agent's current turn ends (see held.py).
    `images` are file paths: a cloud conversation receives the images themselves, an agent
    on this machine a saved copy of each, named in the text, and an agent on another machine
    a copy its bridge fetches from the relay.
    `tags` apply to a message to USER only: the person filters their inbox by them.
    """
    from .. import cloud_send, images as attached
    message_id = message_id or uuid.uuid4().hex
    if when not in held.WHEN:
        raise LiveError(f"when must be one of {', '.join(held.WHEN)}, got {when!r}")
    if when == "idle" and fyi:
        raise LiveError("An fyi never interrupts the receiver; send it without when=idle.")
    if target == USER:
        if fyi or images:
            raise LiveError("A message to user is a note in the person's inbox: send text only, without fyi or images.")
        if when == "idle":
            raise LiveError("A note to user never interrupts anyone; send it without when=idle.")
        return _send_user(text, sender, origin, list(tags), message_id)
    if tags:
        raise LiveError("Tags label messages to user only.")
    if share.is_link(target):
        if fyi or images or when == "idle":
            raise LiveError("A share link takes text only, without fyi, images or when=idle.")
        return _send_share(target, text, sender, origin, wait_reply)
    if target.startswith(GUEST):
        if fyi or images or when == "idle":
            raise LiveError("A guest reads text on its share page: send text only, without fyi, images or when=idle.")
        return _send_guest(target, text, sender, origin, message_id)
    try:
        loaded = attached.load(images)
    except attached.ImageError as error:
        raise LiveError(str(error)) from error
    if cloud_send.is_cloud_target(target):
        if when == "idle":
            raise LiveError("A cloud conversation answers each message in turn; send it without when=idle.")
        return _send_cloud(target, text, sender, origin, fyi, reply, message_id, loaded)
    sender_label = sender.address if sender else origin
    node, local = remote_address(target)
    if node is None and target.startswith("@") and named_elsewhere(target):
        # A name no session here holds may belong to a session another machine published.
        node, local = "", target
    if node is not None:
        hub = _hub()
        if hub is None:
            raise LiveError(f"{target} is on another machine; reaching it needs this machine's bridge "
                            "(`one-conv live serve --hub ...`).")
        if node != hub.node:
            return _post_remote(hub, target, text, sender_label, fyi, reply, message_id, loaded, when)
        target = local  # This machine's own NODE/ prefix: deliver here.
    if loaded:
        text += attached.saved_lines(loaded, live_home() / "attachments")
    return _send_one(target, text, sender, origin, fyi, reply, message_id, when)


def named_elsewhere(target: str) -> bool:
    """Whether `@name` is free here while a relay is configured that may know it."""
    try:
        return names.address_for(target) is None and _hub() is not None
    except names.NameRefused as error:
        raise LiveError(str(error)) from error


# A machine's name in a NODE/harness:id address, as the relay accepts it.
NODE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def remote_address(target: str) -> tuple[str | None, str]:
    """Split a NODE/harness:id route to an agent on another machine; anything else stays local."""
    node, sep, local = target.partition("/")
    if sep and NODE_NAME.fullmatch(node) and parse_address(local)[0] is not None:
        return node, local
    return None, target


def _upload(hub, images) -> list[str]:
    """Upload each image to the relay; the receiving bridge fetches them by id."""
    ids = []
    for image in images:
        try:
            ids.append(hub.upload(image.media_type, image.data)["id"])
        except (hubclient.HubRefused, hubclient.HubUnavailable, KeyError, TypeError) as error:
            raise LiveError(f"could not upload {image.path} to the relay: {error}") from error
    return ids


def _post_remote(hub, target: str, text: str, sender: str, fyi: bool, reply: str, message_id: str,
                 images=(), when: str = "now") -> dict:
    """Post a direct message to a session on another machine; the relay hands it to that machine's bridge.

    `when="idle"` travels with the message, and the receiving bridge holds it while the
    session is busy; a relay that does not echo it back delivered the message as `now`.

    The message sits in the bridge's outbox from before the request until the relay answers,
    so a crash or an unreachable relay leaves it for the bridge to repost under the same id.
    Images are uploaded first: the queued message names them by id.
    """
    message = {"id": message_id, "to": target, "from": sender, "reply": "none" if fyi else reply,
               "mode": "fyi" if fyi else "push", "text": text}
    # Only a held message carries `when`, so a `now` post is the same payload any relay accepts.
    if when == "idle":
        message["when"] = when
    if images:
        message["attachments"] = _upload(hub, images)
    try:
        receipt = _submit_published(hub, message)
    except (hubclient.HubRefused, hubclient.PendingConflict) as error:
        raise LiveError(str(error)) from error
    if receipt is None:
        return {"to": target, "id": message_id, "from": sender, "via": "hub", "delivery": "hub",
                "status": "queued for the relay: no receipt yet, the bridge will resend it under the same id"}
    row = (receipt.get("recipients") or [{}])[0]
    result = {"to": row.get("to", target), "id": message_id, "from": sender, "via": "hub", "delivery": "hub",
              "status": "stored on the relay" if row.get("status") == "stored" else row.get("status", "stored")}
    if when == "idle" and receipt.get("when") != "idle":
        result["warning"] = "the relay does not carry when=idle yet, so the message is delivered at once"
    return result


def _submit_published(hub, message: dict) -> dict | None:
    """hubclient.submit; a session that started here after the bridge's last publication is published first.

    The relay accepts a sender only once this node published it, and the bridge publishes on a
    timer, so a freshly started agent (one `live spawn` or `live teleport` started) would otherwise
    be refused for its first message.
    """
    try:
        return hubclient.submit(hub, message)
    except hubclient.HubRefused as error:
        sender = message.get("from", "")
        if error.status != 403 or not any(s.address == sender for s in list_all(sender.partition(":")[0])):
            raise
        from .bridge import publish_sessions
        try:
            publish_sessions(hub)
        except (hubclient.HubRefused, hubclient.HubUnavailable):
            raise error from None
        return hubclient.submit(hub, message)


def _send_cloud(target: str, text: str, sender: LiveSession | None, origin: str, fyi: bool,
                reply: str, message_id: str, images=()) -> dict:
    """Post into a saved Claude chat or Cowork session; it answers inline, so the reply comes back in the receipt."""
    from .. import cloud_send
    if fyi:
        raise LiveError("A cloud conversation has no feed to hold a notice; send it without fyi.")
    wrapped = cloud_send.cloud_envelope(text, sender, origin, reply, message_id)
    product, query = cloud_send.split_target(target)
    try:
        delivered = cloud_send.deliver(product, query, wrapped, images=images)
    except cloud_send.CloudSendError as error:
        raise LiveError(str(error)) from error
    return {**delivered, "id": message_id, "from": sender.address if sender else origin,
            "via": "claude.ai", "delivery": product, "status": "replied"}


def _send_one(target: str, text: str, sender: LiveSession | None, origin: str, fyi: bool,
              reply: str, message_id: str, when: str = "now") -> dict:
    """Deliver to a session on this machine, or hold it for the bridge while the session is busy."""
    session = resolve(target, exclude=sender)
    holding = when == "idle" and not fyi and held.is_busy(session)
    if holding and not held.bridge_running():
        raise LiveError(f"{session.address} is busy and only this machine's bridge delivers held messages: "
                        "start it with `one-conv live serve`, or send with when=now.")
    if sender is not None and session.address == sender.address:
        raise LiveError(f"{session.address} is the sending session itself.")
    sender_label = sender.address if sender else origin
    handle = names.name_for(sender.address) if sender is not None else None
    wrapped = envelope(text, sender, origin, "none" if fyi else reply, message_id, handle)
    with _rate_slot(sender_label, session.address):
        if fyi:
            _append_fyi(session.address, sender_label, wrapped)
            status, delivery = "stored FYI", "fyi"
        elif holding:
            held.hold(message_id, session.address, sender_label, wrapped)
            status, delivery = held.held_label(session.address), "held"
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


def _send_share(link: str, text: str, sender: LiveSession | None, origin: str, wait_reply: bool) -> dict:
    """Post to a session someone shared with you, and read its answer back inline unless told not to.

    The rate slot applies: two agents talking through a share could otherwise relay forever.
    Receipts name the link without its secret.
    """
    if not text.strip():
        raise LiveError("A message to a share needs text.")
    try:
        target, guest = share.redacted(link), share.Guest(link)
    except share.ShareError as error:
        raise LiveError(str(error)) from error
    label = sender.address if sender is not None else origin
    try:
        with _rate_slot(label, target):
            seq = guest.post(text)
        replies = guest.replies(seq, share.REPLY_WAIT) if wait_reply else []
    except share.ShareError as error:
        raise LiveError(str(error)) from error
    receipt = {"to": target, "from": label, "via": "share", "delivery": "share",
               "after": replies[-1]["seq"] if replies else seq}
    if not wait_reply:
        return {**receipt, "status": "sent"}
    if not replies:
        return {**receipt, "status": f"no answer within {share.REPLY_WAIT:g}s; send again later to see it"}
    return {**receipt, "status": "replied", "reply": "\n\n".join(r["text"] for r in replies)}


def _send_guest(target: str, text: str, sender: LiveSession | None, origin: str, message_id: str) -> dict:
    """Answer the guest of a share (`live share`): the relay files it for that guest's page.

    Only the shared session itself, or the person at its machine, may answer; the relay checks it.
    The relay rate-limits guests, so the agent-to-agent rate slot does not apply to answering one.
    """
    if not GUEST_ADDRESS.fullmatch(target):
        raise LiveError(f"A guest address is guest:<share id>, as the guest's message names it, not {target!r}.")
    if not text.strip():
        raise LiveError("A message to a guest needs text.")
    hub = _hub()
    if hub is None:
        raise LiveError(f"{target} reads its messages on the relay; this machine has none (`one-conv live serve`).")
    label = sender.address if sender is not None and not sender.id.startswith("?") else origin
    try:
        receipt = hubclient.submit(hub, {"id": message_id, "to": target, "from": label, "text": text})
    except (hubclient.HubRefused, hubclient.PendingConflict) as error:
        raise LiveError(str(error)) from error
    status = ("queued for the relay: no receipt yet, the bridge will resend it under the same id" if receipt is None
              else "stored on the relay for the guest's page")
    return {"to": target, "id": message_id, "from": label, "via": "hub", "delivery": "guest", "status": status}


def _user_store() -> Path:
    """This machine's notes for the person when no relay is configured: one JSON object per line."""
    return live_home() / "user" / "notes.jsonl"


def check_tags(tags: list[str]) -> list[str]:
    """Tags in order without repeats, refused unless each is a lowercase word the relay accepts."""
    unique = list(dict.fromkeys(tags))
    if len(unique) > TAGS_PER_NOTE or not all(TAG.fullmatch(t) for t in unique):
        raise LiveError(f"Tags are at most {TAGS_PER_NOTE} lowercase words (letters, digits, dashes), "
                        f"got {', '.join(unique)}.")
    return unique


def _send_user(text: str, sender: LiveSession | None, origin: str, tags: list[str], message_id: str) -> dict:
    """File a note for the person: on the relay when one is configured, so every machine reads it, else here."""
    tags = check_tags(tags)
    if not text.strip():
        raise LiveError("A message to user needs text.")
    known = sender is not None and not sender.id.startswith("?")
    handle = names.name_for(sender.address) if known else None
    # The relay vouches only for this node's published sessions and the person at its terminal.
    label = sender.address if known else ("user" if origin == "user" else "unknown")
    name = f"@{handle}" if handle else (sender.title if known else ("" if origin in ("user", "unknown") else origin))
    with _rate_slot(sender.address if sender else origin, USER):
        hub = _hub()
        if hub is None:
            _append_note(message_id, label, name, text, tags)
            return {"to": USER, "id": message_id, "from": label, "delivery": "inbox", "status": "stored on this machine"}
        message = {"id": message_id, "to": USER, "from": label, "name": name, "text": text, "tags": tags}
        try:
            receipt = hubclient.submit(hub, message)
        except (hubclient.HubRefused, hubclient.PendingConflict) as error:
            raise LiveError(str(error)) from error
    if receipt is None:
        return {"to": USER, "id": message_id, "from": label, "via": "hub", "delivery": "inbox",
                "status": "queued for the relay: no receipt yet, the bridge will resend it under the same id"}
    return {"to": USER, "id": message_id, "from": label, "via": "hub", "delivery": "inbox", "status": "stored on the relay"}


def _append_note(message_id: str, sender: str, name: str, text: str, tags: list[str]) -> None:
    path = _user_store()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".notes.lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        seq = sum(1 for _ in _read_notes(path)) + 1
        row = {"seq": seq, "id": message_id, "from": sender, "name": name, "text": text, "tags": tags,
               "created": time.time()}
        with path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")


def _read_notes(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A torn last line is skipped; the next append numbers past it.
        if isinstance(row, dict) and isinstance(row.get("seq"), int):
            rows.append(row)
    return rows


def user_notes(after: int = 0, tags=(), limit: int = NOTES_LIMIT) -> dict:
    """{"messages": notes after `after` oldest first, "last": newest seq, "source": relay URL or "local"}.

    `tags` keeps notes carrying any of them.
    """
    tags = check_tags(list(tags))
    limit = max(1, min(limit, NOTES_LIMIT))
    hub = _hub()
    if hub is None:
        rows = _read_notes(_user_store())
        kept = [r for r in rows if r["seq"] > after and (not tags or set(tags) & set(r.get("tags") or []))]
        return {"messages": kept[:limit], "last": max((r["seq"] for r in rows), default=0), "source": "local"}
    query = urlencode([("after", max(0, after)), ("limit", limit), *(("tag", t) for t in tags)])
    try:
        page = hub.request("GET", f"/v1/user?{query}")
    except (hubclient.HubRefused, hubclient.HubUnavailable) as error:
        raise LiveError(str(error)) from error
    return {"messages": page.get("messages", []), "last": page.get("last", 0), "source": hub.url}


def latest_notes(tags=(), limit: int = 50) -> dict:
    """The newest `limit` notes carrying any of `tags`, oldest first, paging through the inbox."""
    tail: deque = deque(maxlen=max(1, limit))
    after, last = 0, 0
    while True:
        page = user_notes(after, tags, NOTES_LIMIT)
        tail.extend(page["messages"])
        last = page["last"]
        if len(page["messages"]) < NOTES_LIMIT:
            return {"messages": list(tail), "last": last, "source": page["source"]}
        after = page["messages"][-1]["seq"]
