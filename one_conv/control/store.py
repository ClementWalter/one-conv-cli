"""Persist the personal workspace independently of any operator laptop or relay."""

import hashlib
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from ..live.hub import MESSAGE_ID, TOPIC, Hub, HubError, split_address
from ..live.hub import NODE as PEER_NODE
from .presence import message_activity, message_delivery, session_presence

NODE = "workspace"
HUMAN = "workspace/user"
TEXT_LIMIT = 32_000
MESSAGES_SHOWN = 300


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
    return "user" if address == HUMAN else address


def validate_message(payload: object) -> dict:
    """The browser supplies content and a stable retry ID, never a sender identity."""
    if not isinstance(payload, dict) or set(payload) != {"id", "target", "text"}:
        raise HubError(400, "Provide only id, target and text; the sender is always You.")
    ident, target, text = (payload[key] for key in ("id", "target", "text"))
    if not isinstance(ident, str) or not MESSAGE_ID.fullmatch(ident):
        raise HubError(400, "id must be 32 lowercase hexadecimal characters, unchanged on retry")
    if not isinstance(text, str) or not text.strip():
        raise HubError(400, "Write a nonempty message.")
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
    return {"id": ident, "target": target, "text": text}


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
        with self.hub._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS staffing_grants "
                       "(node TEXT PRIMARY KEY, token_hash TEXT NOT NULL, granted REAL NOT NULL)")
        self.refresh_human()

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
            contacts = db.execute(
                "SELECT p.from_node,p.sender FROM posts p JOIN deliveries d ON d.post=p.seq "
                "WHERE p.topic IS NULL AND d.to_node=? AND d.to_address='user' "
                "UNION SELECT d.to_node,d.to_address FROM posts p JOIN deliveries d ON d.post=p.seq "
                "WHERE p.topic IS NULL AND p.from_node=? AND p.sender='user'", (NODE, NODE)).fetchall()
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
            "topics": [{**topic, "members": [display_address(member) for member in topic["members"]]}
                       for topic in self.hub.topics()],
            "bridge": {"node": NODE, "running": True, "mode": "cloud"}, "error": None}

    @staticmethod
    def _activity(db):
        """Use the last persisted interaction per route without interpreting message contents as a task."""
        rows = db.execute(
            "WITH routes AS (SELECT from_node AS node,sender AS local,seq FROM posts UNION ALL "
            "SELECT to_node,to_address,post FROM deliveries), "
            "latest AS (SELECT node,local,MAX(seq) AS seq FROM routes GROUP BY node,local) "
            "SELECT l.node,l.local,p.id,p.from_node,p.sender,p.target,p.topic,substr(p.text,1,241),p.created "
            "FROM latest l JOIN posts p ON p.seq=l.seq").fetchall()
        return {f"{node}/{local}": {"id": ident, "at": created, "topic": topic, "text": text,
                                   "direction": "outgoing" if (node, local) == (sender_node, sender) else "incoming",
                                   "peer": display_address(target if (node, local) == (sender_node, sender)
                                                           else f"{sender_node}/{sender}")}
                for node, local, ident, sender_node, sender, target, topic, text, created in rows}

    @staticmethod
    def _row(seq, ident, node, sender, target, topic, reply, mode, text, created, outcome, outcome_at=None):
        return {"cursor": str(seq), "id": ident, "from": display_address(f"{node}/{sender}"),
                "to": display_address(target), "topic": topic, "reply": reply, "mode": mode,
                "text": text, "at": created, "status": outcome or "stored",
                "delivery": message_delivery(outcome, outcome_at, human_inbox=target == HUMAN)}

    def direct(self):
        self.drain_human()
        with self.hub._db() as db:
            rows = db.execute(
                "SELECT p.seq,p.id,p.from_node,p.sender,d.to_node || '/' || d.to_address,p.topic,p.reply,p.mode,"
                "p.text,p.created,d.outcome,d.outcome_at FROM posts p JOIN deliveries d ON d.post=p.seq "
                "WHERE p.topic IS NULL ORDER BY p.seq DESC LIMIT ?", (MESSAGES_SHOWN,)).fetchall()
        threads = defaultdict(list)
        for values in reversed(rows):
            row = self._row(*values)
            pair = " ".join(sorted((row["from"], row["to"])))
            threads[pair].append(row)
        return {"threads": [{"pair": pair, "messages": rows, "last": rows[-1]["at"]}
                            for pair, rows in sorted(threads.items(), key=lambda item: -item[1][-1]["at"])]}

    def topic(self, topic: str):
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "Choose a valid topic.")
        with self.hub._db() as db:
            total = db.execute("SELECT COUNT(*) FROM posts WHERE topic=?", (topic,)).fetchone()[0]
            rows = db.execute(
                "SELECT seq,id,from_node,sender,target,topic,reply,mode,text,created,NULL "
                "FROM posts WHERE topic=? ORDER BY seq DESC LIMIT ?", (topic, MESSAGES_SHOWN)).fetchall()
        return {"topic": topic, "messages": [self._row(*row) for row in reversed(rows)],
                "omitted": max(0, total - len(rows))}

    def send(self, payload):
        """Retrying after any crash reuses the hub's atomic (node, ID, payload) claim."""
        payload = validate_message(payload)
        receipt = self.hub.send(NODE, {"id": payload["id"], "from": "user", "to": payload["target"],
                                       "text": payload["text"], "reply": "expected", "mode": "push"},
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
