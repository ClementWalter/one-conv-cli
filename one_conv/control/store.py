"""Persist the personal workspace independently of any operator laptop or relay."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from ..live import unread
from ..live.hub import (
    ATTACHMENT_ID,
    ATTACHMENTS_PER_MESSAGE,
    GENERAL,
    MESSAGE_ID,
    TOPIC,
    Hub,
    HubError,
    _online,
    split_address,
)
from ..live.hub import NODE as PEER_NODE
from .routines import Routines
from .presence import (
    channel_delivery,
    message_activity,
    message_delivery,
    session_presence,
)

NODE = "workspace"
HUMAN = "workspace/user"
TEXT_LIMIT = 32_000
MESSAGES_SHOWN = 300
MEMBERS_LIMIT = 50
# How long a stop request waits for the agent's bridge to report; its inbox long-poll picks orders up within a second.
STOP_WAIT = 10.0


@contextmanager
def private_database(path: Path):
    """Keep browser credentials private even under a permissive process umask."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(path, timeout=10)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def display_address(address: str) -> str:
    """The workspace has one human: the paired mailbox and any machine's terminal are both You."""
    return "user" if address == HUMAN or address.endswith("/user") else address


def validate_message(payload: object) -> dict:
    """The browser supplies content, a stable retry ID and the images it uploaded, never a sender identity."""
    if not isinstance(payload, dict) or not {"id", "target", "text"} <= set(payload) <= {"id", "target", "text", "attachments"}:
        raise HubError(400, "Provide only id, target, text and attachments; the sender is always You.")
    ident, target, text = (payload[key] for key in ("id", "target", "text"))
    attachments = payload.get("attachments") or []
    if (not isinstance(attachments, list) or len(attachments) > ATTACHMENTS_PER_MESSAGE
            or not all(isinstance(a, str) and ATTACHMENT_ID.fullmatch(a) for a in attachments)):
        raise HubError(400, f"attachments lists at most {ATTACHMENTS_PER_MESSAGE} uploaded image ids.")
    if not isinstance(ident, str) or not MESSAGE_ID.fullmatch(ident):
        raise HubError(400, "id must be 32 lowercase hexadecimal characters, unchanged on retry")
    if not isinstance(text, str) or (not text.strip() and not attachments):
        raise HubError(400, "Write a nonempty message or attach an image.")
    if len(text) > TEXT_LIMIT:
        raise HubError(413, "Message text is too large.")
    if not isinstance(target, str) or not target or len(target) > 512:
        raise HubError(400, "Choose a published agent route or #topic.")
    try:
        text.encode("utf-8")
        target.encode("utf-8")
    except UnicodeError:
        raise HubError(400, "Message text and target must contain valid Unicode.") from None
    if target.startswith("#"):
        if not TOPIC.fullmatch(target[1:]):
            raise HubError(400, "Choose a valid #topic.")
    else:
        split_address(target)
        if re.search(r"\s", target):
            raise HubError(400, "An agent route cannot contain whitespace.")
    return {"attachments": attachments, "id": ident, "target": target, "text": text}


def validate_channel(payload: object) -> dict:
    """A new channel: a stable retry ID, its name, a kickoff message and the agents to add."""
    if not isinstance(payload, dict) or set(payload) != {"id", "topic", "purpose", "members"}:
        raise HubError(400, "Provide only id, topic, purpose and members.")
    ident, topic, purpose, members = (payload[key] for key in ("id", "topic", "purpose", "members"))
    if not isinstance(ident, str) or not MESSAGE_ID.fullmatch(ident):
        raise HubError(400, "id must be 32 lowercase hexadecimal characters, unchanged on retry")
    if not isinstance(topic, str) or not TOPIC.fullmatch(topic):
        raise HubError(400, "A channel name uses lowercase letters, digits and dashes.")
    if not isinstance(purpose, str) or not purpose.strip():
        raise HubError(400, "Say what this channel is for; it is posted as the first message.")
    if len(purpose) > TEXT_LIMIT:
        raise HubError(413, "The channel purpose is too large.")
    if (not isinstance(members, list) or len(members) > MEMBERS_LIMIT
            or any(not isinstance(member, str) or member == HUMAN for member in members)
            or len(set(members)) != len(members)):
        raise HubError(400, f"members is a list of at most {MEMBERS_LIMIT} distinct agent routes.")
    return {"id": ident, "topic": topic, "purpose": purpose,
            "members": [split_address(member) for member in members]}


def _provenance(row: dict) -> dict:
    """Say where a reported row comes from: the session's own terminal, or the sending machine's message log."""
    if row.get("reported") and "user" in (row["from"], row["to"]):
        # A turn of the conversation held in the terminal: there was no delivery to report on.
        row["mirrored"] = True
    elif row.get("reported"):
        row["delivery"] = {**row["delivery"], "detail": row["delivery"]["detail"] + (
            " From the sending machine's local message log; nobody was notified again. Older local copies may"
            " be shortened to 20,000 characters.")}
    return row


class ControlStore:
    """The hub posts are the durable mailbox and the idempotent send journal."""

    def __init__(self, directory: Path, hub: Hub | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hub is None:
            path = self.directory / "hub.sqlite3"
            with private_database(path):
                pass
            hub = Hub(str(path))
        else:
            path = Path(hub.path).resolve()
            if not path.is_relative_to(self.directory.resolve()):
                raise ValueError("The cloud hub database must be inside store_dir")
            with private_database(path):
                pass
        self.hub = hub
        # The paired human is the trusted human: its posts wake every running session and are acked.
        hub.adopt_trusted_human(NODE)
        with self.hub._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS staffing_grants "
                       "(node TEXT PRIMARY KEY, token_hash TEXT NOT NULL, granted REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS channel_requests (id TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                       "topic TEXT NOT NULL, announcement TEXT NOT NULL, created REAL NOT NULL)")
            unread.ensure(db)
            # #general is the human's default channel; sessions join it like any other.
            db.execute("INSERT OR IGNORE INTO members (topic, node, address, joined) VALUES (?, ?, 'user', ?)",
                       (GENERAL, NODE, time.time()))
        self.refresh_human()
        # Routines post in their channel exactly as the human's own messages do.
        self.routines = Routines(hub, self.send)

    @staticmethod
    def _operator_node(node):
        if not isinstance(node, str) or not PEER_NODE.fullmatch(node) or node == NODE:
            raise HubError(400, "Choose a lowercase node name using letters, digits and dashes; workspace is reserved")

    def add_node(self, node: str, *, staffing=False) -> str:
        """Replacing a credential removes its delegation unless the operator explicitly grants it."""
        self._operator_node(node)
        if type(staffing) is not bool:
            raise ValueError("staffing must be a boolean")
        token = secrets.token_urlsafe(32)
        digest, now = hashlib.sha256(token.encode()).hexdigest(), time.time()
        with self.hub.lock, self.hub._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO tokens VALUES (?, ?, ?)", (node, digest, now))
            db.execute("DELETE FROM staffing_grants WHERE node = ?", (node,))
            if staffing:
                db.execute("INSERT INTO staffing_grants VALUES (?, ?, ?)", (node, digest, now))
        return token

    def revoke_node(self, node: str) -> bool:
        """Remove authority and published routes together so reused identities start unprivileged."""
        self._operator_node(node)
        with self.hub.lock, self.hub._db() as db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute("DELETE FROM tokens WHERE node = ?", (node,)).rowcount
            db.execute("DELETE FROM staffing_grants WHERE node = ?", (node,))
            db.execute("DELETE FROM sessions WHERE node = ?", (node,))
            db.execute("DELETE FROM members WHERE node = ?", (node,))
            db.execute("DELETE FROM welcomed WHERE node = ?", (node,))
            db.execute("DELETE FROM nodes WHERE node = ?", (node,))
        return bool(removed)

    def grant_staffing(self, node: str):
        """Only the operator can delegate staffing to the currently active credential."""
        self._operator_node(node)
        with self.hub.lock, self.hub._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT hash FROM tokens WHERE node = ?", (node,)).fetchone()
            if row is None:
                raise HubError(404, "No active node credential has that name")
            db.execute("INSERT OR REPLACE INTO staffing_grants VALUES (?, ?, ?)", (node, row[0], time.time()))

    def revoke_staffing(self, node: str):
        """Revoke staffing without interrupting this credential's messaging or accounting."""
        self._operator_node(node)
        with self.hub.lock, self.hub._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM tokens WHERE node = ?", (node,)).fetchone() is None:
                raise HubError(404, "No active node credential has that name")
            db.execute("DELETE FROM staffing_grants WHERE node = ?", (node,))

    def staffing_node(self, request) -> str:
        """A request must present the exact active credential generation delegated by the operator."""
        node = self.hub.authenticated_node(request)
        token = request.headers.getlist("authorization")[0].partition(" ")[2]
        digest = hashlib.sha256(token.encode()).hexdigest()
        # One snapshot prevents an old authenticated request borrowing a replacement token's grant.
        with self.hub._db() as db:
            granted = db.execute("SELECT 1 FROM tokens AS t JOIN staffing_grants AS g "
                                 "ON g.node = t.node AND g.token_hash = t.hash "
                                 "WHERE t.node = ? AND t.hash = ?", (node, digest)).fetchone()
        if granted is None:
            raise HubError(403, "Node staffing authority is required; ask the workspace operator to grant it")
        return node

    def refresh_human(self):
        """A cloud human has a published return route without a local bridge."""
        self.hub.publish(NODE, [{"address": "user", "name": None, "title": "You", "status": "human"}])
        self.drain_human()

    def drain_human(self):
        """Accepted means durably in this mailbox, never that a person has read it."""
        while True:
            items = self.hub.inbox(NODE, 0)
            for item in items:
                if item["to"] == "user":
                    self.hub.ack(NODE, item["delivery"], "accepted")
                else:
                    self.hub.ack(NODE, item["delivery"], "refused")
            if len(items) < 50:
                return

    def state(self):
        self.refresh_human()
        directory = self.hub.directory()
        with self.hub._db() as db:
            activity = self._activity(db)
            seen = dict(db.execute("SELECT node, seen FROM nodes"))
            baseline, _ = unread.marks(db)
            # Channel posts after each topic's mark that someone other than the human sent.
            unseen = dict(db.execute(
                "SELECT p.topic, COUNT(*) FROM posts p WHERE p.topic IS NOT NULL AND p.target LIKE '#%' "
                "AND p.sender != 'user' AND p.created > IFNULL((SELECT r.at FROM read_marks r "
                "WHERE r.conversation = '#' || p.topic), ?) GROUP BY p.topic", (baseline,)))
            contacts = db.execute(
                "SELECT p.from_node,p.sender FROM posts p JOIN deliveries d ON d.post=p.seq "
                "WHERE p.topic IS NULL AND p.kind IS NOT 'system' AND d.to_node=? AND d.to_address='user' "
                "UNION SELECT d.to_node,d.to_address FROM posts p JOIN deliveries d ON d.post=p.seq "
                # A stop order is the hub's instruction, not a conversation with the stopped agent.
                "WHERE p.topic IS NULL AND p.kind IS NOT 'system' AND p.from_node=? AND p.sender='user'",
                (NODE, NODE)).fetchall()
        published = {row["address"] for row in directory}
        # Conversation identities survive disappearing publications without inventing an alias or live process.
        for node, local in contacts:
            address = f"{node}/{local}"
            if address not in published and (":" in local or local == "user"):
                directory.append({"address": address, "node": node, "local": local, "name": None, "title": "",
                                  "cwd": "", "status": "", "seen": seen.get(node), "published_at": None,
                                  "online": False, "history_only": True})
                published.add(address)
        now = time.time()
        return {"at": now, "sessions": [
            {**row, "harness": row["local"].partition(":")[0],
             "id": row["local"].partition(":")[2], "delivery": "hub", "history_only": row.get("history_only", False),
             "presence": session_presence(row, now), "activity": message_activity(activity.get(row["address"]))}
            for row in directory if row["address"] != HUMAN],
            "names": {row["name"]: display_address(row["address"]) for row in directory if row["name"]},
            "topics": [{**topic, "members": [display_address(member) for member in topic["members"]],
                        "participants": [display_address(route) for route in topic["participants"]],
                        "unread": unseen.get(topic["topic"], 0)}
                       for topic in self.hub.topics()],
            "routines": self.routines.listing(),
            "bridge": {"node": NODE, "running": True, "mode": "cloud"}, "error": None}

    @staticmethod
    def _activity(db):
        """Use the last persisted interaction per route without interpreting message contents as a task."""
        rows = db.execute(
            # The hub's own assignment notices are nobody's activity.
            "WITH routes AS (SELECT from_node AS node,sender AS local,seq FROM posts WHERE kind IS NOT 'system' "
            "UNION ALL SELECT d.to_node,d.to_address,d.post FROM deliveries d JOIN posts p ON p.seq=d.post "
            "WHERE p.kind IS NOT 'system'), "
            "latest AS (SELECT node,local,MAX(seq) AS seq FROM routes GROUP BY node,local) "
            "SELECT l.node,l.local,p.id,p.from_node,p.sender,p.target,p.topic,substr(p.text,1,241),p.created "
            "FROM latest l JOIN posts p ON p.seq=l.seq").fetchall()
        return {f"{node}/{local}": {"id": ident, "at": created, "topic": topic, "text": text,
                                   "direction": "outgoing" if (node, local) == (sender_node, sender) else "incoming",
                                   "peer": display_address(target if (node, local) == (sender_node, sender)
                                                           else f"{sender_node}/{sender}")}
                for node, local, ident, sender_node, sender, target, topic, text, created in rows}

    @staticmethod
    def _row(seq, ident, node, sender, target, topic, reply, mode, text, created, outcome, outcome_at=None, reported=0,
             attachments=None):
        row = {"cursor": str(seq), "id": ident, "from": display_address(f"{node}/{sender}"),
               "to": display_address(target), "topic": topic, "reply": reply, "mode": mode,
               "text": text, "at": created, "status": outcome or "stored",
               "delivery": message_delivery(outcome, outcome_at, human_inbox=target == HUMAN)}
        if reported:
            row["reported"] = True
        if attachments:
            row["attachments"] = Hub._parsed(attachments)
        return row

    def store_attachment(self, media_type: str, body: bytes) -> dict:
        return self.hub.store_attachment(NODE, media_type, body)

    def attachment(self, ident: str) -> tuple[str, bytes]:
        return self.hub.attachment(ident)

    def direct(self):
        self.drain_human()
        with self.hub._db() as db:
            rows = db.execute(
                "SELECT p.seq,p.id,p.from_node,p.sender,d.to_node || '/' || d.to_address,p.topic,p.reply,p.mode,"
                "p.text,p.created,d.outcome,d.outcome_at,p.reported,p.attachments FROM posts p JOIN deliveries d ON d.post=p.seq "
                # Directness comes from the original target, so a topic-tagged DM stays in its pair's thread.
                # Only conversations with the human are shown; agents' messages to each other are theirs. The human
                # writes from the paired workspace or from a machine's terminal, whose bridge reports the exchange.
                "WHERE p.target NOT LIKE '#%' AND p.kind IS NOT 'system' AND (p.sender = 'user' OR d.to_address = 'user')"
                " ORDER BY p.created DESC, p.seq DESC LIMIT ?", (MESSAGES_SHOWN,)).fetchall()
            baseline, marks = unread.marks(db)
            shown = [values[0] for values in rows]
            acks = self.hub._acks(db, shown)
        threads = defaultdict(list)
        for values in reversed(rows):
            row = _provenance(self._row(*values))
            self._attach(row, values[0], acks)
            pair = " ".join(sorted((row["from"], row["to"])))
            threads[pair].append(row)
        return {"threads": [{"pair": pair, "messages": rows, "last": rows[-1]["at"],
                             # An exchange a terminal reported was read at that terminal.
                             "unread": unread.count([row for row in rows if not (row.get("reported")
                                                     and "user" in (row["from"], row["to"]))], marks.get(pair, baseline))}
                            for pair, rows in sorted(threads.items(), key=lambda item: -item[1][-1]["at"])]}

    def mark_read(self, payload):
        """Record that the human has seen a conversation up to a send time, on every device."""
        try:
            payload = unread.validate(payload)
        except unread.ReadError as error:
            raise HubError(400, str(error)) from None
        with self.hub.lock, self.hub._db() as db:
            return {"conversation": payload["conversation"], "at": unread.mark(db, payload)}

    def fingerprint(self) -> str:
        """A digest of everything the workspace shows; it changes when a post, receipt, member, agent or read mark does.

        Read-only. The workspace's own human publication is left out, since reading state renews it.
        """
        with self.hub._db() as db:
            values = db.execute(
                "SELECT (SELECT IFNULL(MAX(seq), 0) FROM posts),"
                " (SELECT COUNT(*) FROM deliveries WHERE outcome IS NOT NULL),"
                " (SELECT IFNULL(group_concat(row, ','), '') FROM (SELECT topic || '/' || node || '/' || address || '@'"
                "  || joined AS row FROM members ORDER BY topic, node, address)),"
                " (SELECT IFNULL(group_concat(row, ','), '') FROM (SELECT node || '/' || address || '=' ||"
                "  IFNULL(name, '') || ':' || IFNULL(status, '') || ':' || updated AS row FROM sessions"
                "  WHERE node != ? ORDER BY node, address)),"
                " (SELECT IFNULL(group_concat(row, ','), '') FROM (SELECT conversation || '=' || at AS row"
                "  FROM read_marks ORDER BY conversation)),"
                # Staffing progress (a session starting on a machine or in the cloud) shows under the post.
                " (SELECT IFNULL(group_concat(row, ','), '') FROM (SELECT task || '@' || version || '=' || state"
                "  AS row FROM staffing ORDER BY task, version)),"
                # A session acking a post adds a check mark under it; archiving moves a channel.
                " (SELECT COUNT(*) FROM post_acks),"
                " (SELECT IFNULL(group_concat(topic, ','), '') FROM archived_topics),"
                # A routine saved, paused or fired changes its row in the list.
                " (SELECT IFNULL(group_concat(row, ','), '') FROM (SELECT id || '@' || updated || '/' || enabled || '/'"
                "  || IFNULL(next_at, '') || '/' || IFNULL(last_at, '') || '/' || last_error AS row FROM routines"
                "  ORDER BY id))", (NODE,)).fetchone()
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:32]

    def topic(self, topic: str):
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "Choose a valid topic.")
        with self.hub._db() as db:
            # The hub's own instructions (spawn orders, task notices) are never shown as messages.
            total = db.execute("SELECT COUNT(*) FROM posts WHERE topic=? AND kind IS NOT 'system'",
                               (topic,)).fetchone()[0]
            rows = db.execute(
                "SELECT seq,id,from_node,sender,target,topic,reply,mode,text,created,NULL,NULL,reported,attachments "
                # Reported history keeps its original send time, so time orders the channel.
                "FROM posts WHERE topic=? AND kind IS NOT 'system' ORDER BY created DESC, seq DESC LIMIT ?",
                (topic, MESSAGES_SHOWN)).fetchall()
            recipients = defaultdict(list)
            shown = [row[0] for row in rows]
            acks = self.hub._acks(db, shown)
            # A session being started for a post nobody was running to read.
            starting = {key: {"route": route, "state": state, "detail": detail} for key, route, state, detail in
                        db.execute("SELECT task, route, state, detail FROM staffing WHERE state != 'done'")}
            if rows:
                for post, route, outcome, outcome_at in db.execute(
                        "SELECT post, to_node || '/' || to_address, outcome, outcome_at FROM deliveries "
                        f"WHERE post IN ({','.join('?' * len(shown))}) ORDER BY rowid", shown):
                    recipients[post].append((display_address(route), outcome, outcome_at))
        messages = []
        for row in reversed(rows):
            message = self._row(*row)
            # A channel post has one delivery per member, so its status is their rollup.
            message["delivery"] = channel_delivery(recipients[row[0]])
            message["status"] = message["delivery"]["state"]
            # A topic-tagged direct message is listed here for context; only its target received it.
            message["directed"] = not row[4].startswith("#")
            self._attach(message, row[0], acks)
            if f"{row[2]}:{row[1]}" in starting:
                message["staffing"] = starting[f"{row[2]}:{row[1]}"]
            messages.append(_provenance(message))
        return {"topic": topic, "messages": messages, "omitted": max(0, total - len(rows))}

    @staticmethod
    def _attach(message: dict, seq: int, acks: dict) -> None:
        """Who read the message, as the sessions that acked it."""
        if seq in acks:
            message["acks"] = [display_address(route) for route in acks[seq]]

    def send(self, payload):
        """Retrying after any crash reuses the hub's atomic (node, ID, payload) claim."""
        payload = validate_message(payload)
        receipt = self.hub.send(NODE, {"id": payload["id"], "from": "user", "to": payload["target"],
                                       "text": payload["text"], "reply": "expected", "mode": "push",
                                       "attachments": payload["attachments"]},
                                join_sender=payload["target"].startswith("#"),
                                require_online=not payload["target"].startswith("#"))
        recipients = receipt["recipients"]
        unconfirmed = any(row["status"] in ("refused", "unconfirmed") for row in recipients)
        return {"id": payload["id"], "status": "unconfirmed" if unconfirmed else "sent", "receipt": {
            "id": payload["id"], "from": "user", "to": payload["target"], "via": "hub", "delivery": "hub",
            "status": "delivery unconfirmed" if unconfirmed else "stored on the hub", "duplicate": receipt["duplicate"],
            "deliveries": [{"to": display_address(row["to"]), "status": row["status"], "delivery": row["delivery"]}
                           for row in recipients],
            **({"topic": payload["target"][1:]} if payload["target"].startswith("#") else {}),
            "notice": "Stored durably; delivery acceptance does not mean the recipient has read it."}}

    def stop_session(self, payload, wait: float | None = None):
        """The human ends an agent the spawner started; answers stopped, refused or pending."""
        if not isinstance(payload, dict) or set(payload) != {"address"} or not isinstance(payload["address"], str):
            raise HubError(400, "Provide the address of the agent to stop.")
        order = self.hub.stop(NODE, payload["address"])
        deadline = time.monotonic() + (STOP_WAIT if wait is None else wait)
        outcome = self.hub.stop_outcome(NODE, order["id"])
        while outcome is None and time.monotonic() < deadline:
            time.sleep(0.25)
            outcome = self.hub.stop_outcome(NODE, order["id"])
        status = {"accepted": "stopped", "refused": "refused", "unconfirmed": "unconfirmed"}.get(outcome, "pending")
        return {"address": display_address(payload["address"]), "status": status, "id": order["id"]}

    def archive_channel(self, payload):
        """The human hides a channel from the list, or brings it back; agents still find and read it."""
        if not isinstance(payload, dict) or set(payload) != {"topic", "archived"} or type(payload["archived"]) is not bool:
            raise HubError(400, "Provide topic and archived (true or false).")
        if not isinstance(payload["topic"], str) or not TOPIC.fullmatch(payload["topic"]):
            raise HubError(400, "Choose a valid channel.")
        return self.hub.archive(payload["topic"], payload["archived"])

    def create_channel(self, payload):
        """Add the human and the chosen agents, post the kickoff in the channel and announce it in #general.

        The request, its members and the announcement text commit together, so a retry under the same
        ID finishes exactly that channel, and the same ID with any other content is refused.
        """
        payload = validate_channel(payload)
        topic, ident = payload["topic"], payload["id"]
        digest = hashlib.sha256(json.dumps([topic, payload["purpose"], sorted(payload["members"])]).encode()).hexdigest()
        with self.hub.lock, self.hub._db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute("SELECT digest, announcement FROM channel_requests WHERE id = ?", (ident,)).fetchone()
            if request is not None and request[0] != digest:
                raise HubError(409, "This request ID was already used for a different channel; start a new request.")
            if request is None:
                if (db.execute("SELECT 1 FROM members WHERE topic = ?", (topic,)).fetchone()
                        or db.execute("SELECT 1 FROM posts WHERE topic = ?", (topic,)).fetchone()):
                    raise HubError(409, f"#{topic} already exists; open it from Channels.")
                now = time.time()
                for node, local in payload["members"]:
                    lease = db.execute("SELECT n.seen, s.updated FROM sessions s JOIN nodes n ON n.node = s.node "
                                       "WHERE s.node = ? AND s.address = ?", (node, local)).fetchone()
                    if lease is None or not _online(*lease, now):
                        raise HubError(404, f"{node}/{local} is not connected; reconnect it or leave it out.")
                names = {(node, address): name for node, address, name in
                         db.execute("SELECT node, address, name FROM sessions WHERE name IS NOT NULL")}
                who = ", ".join("@" + names[member] if member in names else f"{member[0]}/{member[1]}"
                                for member in payload["members"]) or "only you so far"
                summary = payload["purpose"].strip().splitlines()[0][:200]
                announcement = (f"You created #{topic}: {summary}\nMembers: {who}.\n"
                                f"Anyone else can join with the join_topic tool or `one-conv live join {topic}`.")
                db.execute("INSERT INTO channel_requests VALUES (?, ?, ?, ?, ?)", (ident, digest, topic, announcement, now))
                # The workspace owner places agents directly, as their work channel; they can still leave.
                db.executemany("INSERT OR IGNORE INTO members (topic, node, address, joined) VALUES (?, ?, ?, ?)",
                               [(topic, NODE, "user", now)]
                               + [(topic, node, local, now) for node, local in payload["members"]])
                db.executemany("UPDATE members SET work = (topic = ?) WHERE node = ? AND address = ?",
                               [(topic, node, local) for node, local in payload["members"]])
            else:
                announcement = request[1]
        kickoff = self.hub.send(NODE, {"id": ident, "from": "user", "to": f"#{topic}", "text": payload["purpose"],
                                       "reply": "expected", "mode": "push"}, join_sender=True)
        self.hub.send(NODE, {"id": hashlib.sha256(f"{ident}:{GENERAL}".encode()).hexdigest()[:32], "from": "user",
                             "to": f"#{GENERAL}", "text": announcement, "reply": "none", "mode": "push"},
                      join_sender=True)
        return {"id": ident, "topic": topic, "duplicate": kickoff["duplicate"],
                "members": [display_address(f"{node}/{local}") for node, local in payload["members"]],
                "deliveries": [{"to": display_address(row["to"]), "status": row["status"]}
                               for row in kickoff["recipients"]]}
