"""The live hub: routes messages between agents on different machines.

Each machine runs one bridge (`one-conv live serve`) that authenticates with its own node
token, publishes its live sessions, and long-polls for deliveries addressed to them.

A message is a post, kept once under (sending node, client-generated id) with a hash of its
payload, so a retry returns the first receipt and a different payload under the same id is a
conflict. A post becomes one delivery per recipient, each with its own delivery id, which
the receiving bridge acknowledges with an outcome. Topic history is read from posts, so a
post is kept even when no other member is there yet.

A node token vouches for the sessions its bridge publishes, not for each process on that
machine. Only token hashes are stored. Delivery is at least once to the bridge; the bridge's
ingress log is what keeps a retried delivery from prompting an agent twice.

A topic is a channel of work, not a team roster. Its members are the sessions running now that
joined it: membership ends shortly after a session stops being published, while the channel's
posts, and the session that wrote each one, stay for whoever joins later.

A request from a trusted human (a node in `trusted_humans`, sending as "user") becomes a task
in the same commit as its post (see `tasks`). A direct request is owned by its recipient. A
channel request starts unowned and is offered to every running member: the first claim wins
and the others then get it as context. A task whose owner stopped running is released and
offered again. Ownership otherwise changes only through the owner itself (claim, delegate)
or the human (assign), never through the node API on another agent's behalf.
"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import tasks

NODE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MESSAGE_ID = re.compile(r"[0-9a-f]{32}")
# A session on the reporting node itself: harness:id, never a human or another node's route.
LOCAL_AGENT = re.compile(r"[a-z]+:[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
REPORT_BATCH = 100
# What the receiving bridge reports: the adapter accepted it (not that the agent read it),
# the adapter refused it, or the bridge cannot tell (it crashed between claiming and reporting).
OUTCOMES = ("accepted", "refused", "unconfirmed")
MODES = ("push", "fyi")
# Why a delivery reached its recipient: the task's owner, a collaborator on a child task, or an
# observer who receives the request as context only. Agent-to-agent messages have no role.
# A candidate is offered an unowned request and may claim it.
ROLES = ("owner", "collaborator", "observer", "candidate")
MAX_TEXT = 200_000
# A JSON-escaped text can take up to six bytes per character, plus the envelope fields.
MAX_BODY = MAX_TEXT * 6 + 4096
MAX_WAIT = 30.0
# A node that has not published or polled for this long is listed as offline.
LEASE_SECONDS = 60.0
# A member whose session has not been published for this long leaves its topics; a bridge
# restart is shorter, so a running session keeps its channels.
MEMBER_GRACE_SECONDS = 600.0
# A task whose owner session has not been published for this long is released for others to claim.
OWNER_GRACE_SECONDS = 120.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (node TEXT PRIMARY KEY, hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (node TEXT NOT NULL, address TEXT NOT NULL, name TEXT, title TEXT, cwd TEXT,
    status TEXT, updated REAL NOT NULL, PRIMARY KEY (node, address));
CREATE TABLE IF NOT EXISTS members (topic TEXT NOT NULL, node TEXT NOT NULL, address TEXT NOT NULL,
    joined REAL NOT NULL, PRIMARY KEY (topic, node, address));
CREATE TABLE IF NOT EXISTS posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, topic TEXT, reply TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'push',
    text TEXT NOT NULL, created REAL NOT NULL, UNIQUE (from_node, id));
CREATE INDEX IF NOT EXISTS by_topic ON posts (topic, seq);
CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, post INTEGER NOT NULL REFERENCES posts (seq),
    to_node TEXT NOT NULL, to_address TEXT NOT NULL, taken REAL, outcome TEXT, outcome_at REAL,
    UNIQUE (post, to_node, to_address));
CREATE INDEX IF NOT EXISTS pending ON deliveries (to_node, outcome, post);
CREATE TABLE IF NOT EXISTS welcomed (node TEXT NOT NULL, address TEXT NOT NULL, PRIMARY KEY (node, address));
CREATE TABLE IF NOT EXISTS session_seen (node TEXT NOT NULL, address TEXT NOT NULL, seen REAL NOT NULL,
    PRIMARY KEY (node, address));
"""
GENERAL = "general"


class HubError(Exception):
    """A request the hub refuses, with the HTTP status to answer it with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class TaskRefused(HubError):
    """A refused task action, carrying the task's current state so the caller never guesses it."""

    def __init__(self, error: tasks.TaskError):
        super().__init__(error.status, str(error))
        self.task = error.task


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _online(seen: float, published: float, now: float) -> bool:
    """A polling bridge cannot keep an old session publication alive."""
    return 0 <= now - seen <= LEASE_SECONDS and 0 <= now - published <= LEASE_SECONDS


def split_address(address: str) -> tuple[str, str]:
    """Split an agent or a published human's canonical return route."""
    node, sep, local = address.partition("/")
    if not sep or not NODE.fullmatch(node) or (local != "user" and ":" not in local) or "/" in local.split(":", 1)[0]:
        raise HubError(400, f"a remote address is NODE/harness:id, not {address!r}")
    return node, local


class Hub:
    """The hub's state in one SQLite file, safe for the server's threads."""

    def __init__(self, path: str):
        self.path, self.lock = path, threading.Lock()
        # Nodes whose "user" is an authenticated human, whose requests become tasks (the workspace).
        self.trusted_humans: set[str] = set()
        with self._db() as db:
            db.executescript(SCHEMA)
            tasks.ensure(db)
            posts = {row[1] for row in db.execute("PRAGMA table_info(posts)")}
            # Posts a node reported after delivering them itself; they never had a hub delivery to take.
            if "reported" not in posts:
                db.execute("ALTER TABLE posts ADD COLUMN reported INTEGER NOT NULL DEFAULT 0")
            # The task an agent's post answers or reports on, checked against its current owner.
            if "task" not in posts:
                db.execute("ALTER TABLE posts ADD COLUMN task TEXT")
            # "system" for an instruction the hub issued itself, which no human or agent wrote.
            if "kind" not in posts:
                db.execute("ALTER TABLE posts ADD COLUMN kind TEXT")
            members = {row[1] for row in db.execute("PRAGMA table_info(members)")}
            # When the member's session was last published; membership ends a grace period after it.
            if "seen" not in members:
                db.execute("ALTER TABLE members ADD COLUMN seen REAL")
            deliveries = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            # A delivery can wake its recipient while the same post only informs the others.
            for column in ("role", "mode", "task"):
                if column not in deliveries:
                    db.execute(f"ALTER TABLE deliveries ADD COLUMN {column} TEXT")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # Tokens are managed on the hub's own machine; the server never creates them.
    def add_token(self, node: str) -> str:
        if not NODE.fullmatch(node):
            raise HubError(400, f"node names are lowercase letters, digits and dashes: {node!r}")
        token = secrets.token_urlsafe(32)
        with self.lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO tokens VALUES (?, ?, ?)", (node, _hash(token), time.time()))
        return token

    def revoke(self, node: str) -> bool:
        with self.lock, self._db() as db:
            removed = db.execute("DELETE FROM tokens WHERE node = ?", (node,)).rowcount
            db.execute("DELETE FROM sessions WHERE node = ?", (node,))
            db.execute("DELETE FROM members WHERE node = ?", (node,))
            db.execute("DELETE FROM welcomed WHERE node = ?", (node,))
            db.execute("DELETE FROM nodes WHERE node = ?", (node,))
        return bool(removed)

    def node_for(self, token: str | None) -> str:
        """The node a bearer token belongs to; the request's identity comes only from here."""
        if not token:
            raise HubError(401, "missing bearer token")
        digest = _hash(token)
        with self._db() as db:
            rows = db.execute("SELECT node, hash FROM tokens").fetchall()
        for node, stored in rows:
            if hmac.compare_digest(stored, digest):
                return node
        raise HubError(401, "unknown or revoked token")

    def authenticated_node(self, request) -> str:
        """Authenticate node extensions with bearer authority, never browser cookies."""
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            raise HubError(401, "exactly one bearer authorization header is required")
        scheme, separator, token = values[0].partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or any(char.isspace() for char in token):
            raise HubError(401, "a bearer authorization header is required")
        return self.node_for(token)

    def _seen(self, db, node: str) -> None:
        db.execute("INSERT OR REPLACE INTO nodes VALUES (?, ?)", (node, time.time()))

    def publish(self, node: str, sessions) -> int:
        """Replace this node's published sessions and renew its lease; a name already taken is refused."""
        if not isinstance(sessions, list):
            raise HubError(400, "sessions must be a list")
        rows, names = [], set()
        for item in sessions:
            if not isinstance(item, dict) or not isinstance(item.get("address"), str) or "/" in item["address"]:
                raise HubError(400, "each session needs a local address like claude:<id>")
            name = item.get("name")
            if name is not None and (not isinstance(name, str) or not NODE.fullmatch(name)):
                raise HubError(400, f"a name is lowercase letters, digits and dashes: {name!r}")
            if name is not None:
                if name in names:
                    raise HubError(409, f"@{name} is given to two sessions")
                names.add(name)
            rows.append((node, item["address"], name, str(item.get("title") or ""), str(item.get("cwd") or ""),
                         str(item.get("status") or ""), time.time()))
        with self.lock, self._db() as db:
            for name in names:
                if db.execute("SELECT 1 FROM sessions WHERE name = ? AND node != ?", (name, node)).fetchone():
                    raise HubError(409, f"@{name} is already used on another node")
            db.execute("DELETE FROM sessions WHERE node = ?", (node,))
            db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            now = time.time()
            db.executemany("INSERT OR REPLACE INTO session_seen VALUES (?, ?, ?)", [(node, r[1], now) for r in rows])
            db.executemany("UPDATE members SET seen = ? WHERE node = ? AND address = ?", [(now, node, r[1]) for r in rows])
            self._seen(db, node)
            self._sweep(db, now)
        return len(rows)

    def sweep(self) -> list[dict]:
        """End stale memberships and release tasks whose owner stopped running; the released tasks."""
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._sweep(db, time.time())

    def _sweep(self, db, now: float) -> list[dict]:
        # A session last published before the grace period, on any node, has left its topics.
        db.execute("DELETE FROM members WHERE address != 'user' AND IFNULL(seen, joined) < ? AND NOT EXISTS ("
                   "SELECT 1 FROM sessions s JOIN nodes n ON n.node = s.node WHERE s.node = members.node"
                   " AND s.address = members.address AND n.seen >= ?)",
                   (now - MEMBER_GRACE_SECONDS, now - LEASE_SECONDS))
        released = []
        rows = db.execute(f"SELECT key, owner, updated FROM tasks WHERE parent IS NULL AND owner IS NOT NULL"
                          f" AND state NOT IN ({','.join('?' * len(tasks.FINISHED))})", tasks.FINISHED).fetchall()
        for key, owner, updated in rows:
            if self._available(db, owner, now):
                continue
            node, _, address = owner.partition("/")
            seen = db.execute("SELECT seen FROM session_seen WHERE node = ? AND address = ?", (node, address)).fetchone()
            if max(seen[0] if seen else 0.0, updated) >= now - OWNER_GRACE_SECONDS:
                continue
            task = tasks.release(db, key, "hub")
            self._retire(db, key)
            self._offer(db, task)
            released.append(task)
        return released

    def _offer(self, db, task: dict) -> int:
        """Offer an unowned channel request to the channel's running members; how many were offered."""
        if task["owner"] is not None or not task["topic"]:
            return 0
        now = time.time()
        candidates = [(n, a) for n, a in self._recipients(db, f"#{task['topic']}", "", "")
                      if a != "user" and self._available(db, f"{n}/{a}", now)]
        if not candidates:
            return 0
        msg_id = hashlib.sha256(f"{task['key']}@{task['version']}:offer".encode()).hexdigest()[:32]
        node, _, sender = task["created_by"].partition("/")
        if db.execute("SELECT 1 FROM posts WHERE from_node = ? AND id = ?", (node, msg_id)).fetchone():
            return 0
        original = db.execute("SELECT text FROM posts WHERE seq = ?", (task["post"],)).fetchone()
        text = (f"Request {task['key']} in #{task['topic']} is unclaimed again: the session that held it stopped."
                f"\n{original[0] if original else task['title']}")
        digest = hashlib.sha256(json.dumps(["offer", task["key"], task["version"]]).encode()).hexdigest()
        self._insert(db, node, msg_id, digest, f"#{task['topic']}", sender, task["topic"], "expected", "push", text,
                     [(n, a, "candidate", "push", task["key"]) for n, a in candidates], task=task["key"],
                     kind="system")
        return len(candidates)

    def directory(self) -> list[dict]:
        """Every published session, with independent bridge and publication timestamps."""
        now = time.time()
        with self._db() as db:
            seen = dict(db.execute("SELECT node, seen FROM nodes"))
            rows = db.execute("SELECT node, address, name, title, cwd, status, updated FROM sessions "
                              "ORDER BY node, address").fetchall()
        return [{"address": f"{node}/{address}", "node": node, "local": address, "name": name, "title": title,
                 "cwd": cwd, "status": status, "seen": seen.get(node),
                 "published_at": published, "online": _online(seen.get(node, 0.0), published, now)}
                for node, address, name, title, cwd, status, published in rows]

    def _published(self, db, node: str, address: str) -> bool:
        return db.execute("SELECT 1 FROM sessions WHERE node = ? AND address = ?", (node, address)).fetchone() is not None

    def _recipients(self, db, target: str, from_node: str, sender: str) -> list[tuple[str, str]]:
        """Where a message goes: one session, a named one, or every member of a topic but the sender."""
        if target.startswith("#"):
            # A channel reaches its members whose session is still published; it may have none right now.
            members = db.execute("SELECT m.node, m.address FROM members m WHERE m.topic = ? AND (m.address = 'user'"
                                 " OR EXISTS (SELECT 1 FROM sessions s WHERE s.node = m.node AND s.address = m.address))"
                                 " ORDER BY m.joined", (target[1:],)).fetchall()
            return [(n, a) for n, a in members if (n, a) != (from_node, sender)]
        if target.startswith("@"):
            row = db.execute("SELECT node, address FROM sessions WHERE name = ?", (target[1:],)).fetchone()
            if row is None:
                raise HubError(404, f"no published session is named {target}")
            return [row]
        node, address = split_address(target)
        if not self._published(db, node, address):
            raise HubError(404, f"{target} is not a published session")
        return [(node, address)]

    def _receipt(self, db, seq: int, msg_id: str, duplicate: bool) -> dict:
        rows = db.execute("SELECT delivery, to_node, to_address, outcome, role FROM deliveries WHERE post = ? "
                          "ORDER BY rowid", (seq,)).fetchall()
        receipt = {"id": msg_id, "duplicate": duplicate,
                   "recipients": [{"to": f"{n}/{a}", "delivery": d, "status": o or "stored",
                                   **({"role": role} if role else {})} for d, n, a, o, role in rows]}
        task = db.execute(f"SELECT {tasks.COLUMNS} FROM tasks WHERE post_seq = ? AND parent IS NULL",
                          (seq,)).fetchone()
        if task:
            receipt["task"] = tasks._row(task)
        return receipt

    def _available(self, db, route: str, now: float) -> bool:
        node, _, address = route.partition("/")
        lease = db.execute("SELECT n.seen, s.updated FROM sessions s JOIN nodes n ON n.node = s.node "
                           "WHERE s.node = ? AND s.address = ?", (node, address)).fetchone()
        return lease is not None and _online(*lease, now)

    def _resolve(self, db, who: str) -> str | None:
        """A full route for @name or a route, if that session is published; otherwise None."""
        if who.startswith("@"):
            row = db.execute("SELECT node, address FROM sessions WHERE name = ?", (who[1:],)).fetchone()
            return f"{row[0]}/{row[1]}" if row else None
        node, _, address = who.partition("/")
        return who if self._published(db, node, address) else None

    def _insert(self, db, node: str, msg_id: str, digest: str, target: str, sender: str, topic: str | None,
                reply: str, mode: str, text: str, deliveries, task: str | None = None, kind: str | None = None) -> int:
        """One post and its deliveries; `deliveries` is (node, address, role, mode, task) per recipient.

        `kind="system"` marks an instruction the hub itself issued (an assignment, a release): it
        is delivered like any post but is never shown as something the human or an agent wrote.
        """
        seq = db.execute("INSERT INTO posts (from_node, id, digest, target, sender, topic, reply, mode, text, created,"
                         " task, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (node, msg_id, digest, target, sender, topic, reply, mode, text, time.time(),
                          task, kind)).lastrowid
        db.executemany("INSERT INTO deliveries (delivery, post, to_node, to_address, role, mode, task)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [(secrets.token_hex(16), seq, n, a, role, m, t) for n, a, role, m, t in deliveries])
        return seq

    def _linked(self, db, message: dict, actor: str, target: str) -> str | None:
        """The task a post answers; only its current owner or a granted collaborator may link one.

        A collaborator's output goes to the owner of the request it works on, never straight to
        the human or the channel: the owner is the request's one publisher.
        """
        link = message.get("task")
        if link is None:
            return None
        if not isinstance(link, dict) or set(link) != {"key", "version"}:
            raise HubError(400, "task is {key, version}, as the delivery gave them")
        try:
            task = tasks.require(db, link["key"])
            tasks._fence(task, actor, link["version"])
            if task["state"] in tasks.FINISHED:
                # The answer belongs before the task is closed; a finished task takes no more output.
                raise tasks.TaskError(409, f"{task['key']} is already {task['state']}: post the answer before "
                                           "marking it done, or send without --task if it is about something else",
                                      task)
        except tasks.TaskError as error:
            raise TaskRefused(error) from None
        if task["parent"]:
            owner = (tasks.get(db, task["parent"]) or {}).get("owner")
            if target != owner:
                raise HubError(403, f"{task['key']} is a part of {task['parent']}: report it to that request's owner "
                                    f"({owner or 'nobody yet'}), who answers the human")
        return task["key"]

    def _unanswered(self, db, sender_route: str, target: str, topic: str | None) -> list[dict]:
        """Open human requests someone else owns, that this post would answer by its target.

        A post to the human (beside any open request) or into the channel of an open request
        must say what it is about, so no agent becomes a second responder by leaving the task
        out, whether it received the request, joined later, or never saw it.
        """
        human = target.partition("/")[0] in self.trusted_humans and target.endswith("/user")
        if not human and not target.startswith("#"):
            return []
        rows = db.execute(
            f"SELECT key, owner, topic FROM tasks WHERE parent IS NULL"
            f" AND state NOT IN ({','.join('?' * len(tasks.FINISHED))}) AND IFNULL(owner, '') != ?"
            f" AND created_by IN ({','.join('?' * len(self.trusted_humans))}) ORDER BY created",
            (*tasks.FINISHED, sender_route, *(f"{node}/user" for node in self.trusted_humans))).fetchall()
        return [{"key": key, "owner": owner, "topic": t} for key, owner, t in rows if human or t == topic]

    def send(self, node: str, message: dict, *, join_sender: bool = False, require_online: bool = False) -> dict:
        """Store a post and one delivery per recipient; a retry of the same post returns its first receipt."""
        msg_id, sender, text = message.get("id"), message.get("from"), message.get("text")
        if not isinstance(msg_id, str) or not MESSAGE_ID.fullmatch(msg_id):
            raise HubError(400, "id must be 32 lowercase hex characters, chosen by the sender")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            raise HubError(400, f"text must be a non-empty string of at most {MAX_TEXT} characters")
        reply = message.get("reply", "expected")
        if reply not in ("expected", "none", "inline"):
            raise HubError(400, "reply must be expected, none or inline")
        # push wakes the receiver; fyi only files a notice for it to read when it chooses.
        mode = message.get("mode", "push")
        if mode not in MODES:
            raise HubError(400, f"mode must be one of {', '.join(MODES)}")
        target = message.get("to")
        if not isinstance(target, str) or not target:
            raise HubError(400, "to must be NODE/harness:id, @name or #topic")
        topic = target[1:] if target.startswith("#") else message.get("topic")
        if topic is not None and (not isinstance(topic, str) or not TOPIC.fullmatch(topic)):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        link, about = message.get("task"), message.get("about")
        if about not in (None, "other"):
            raise HubError(400, 'about is "other" for a post that answers no open request')
        digest = hashlib.sha256(json.dumps([target, sender, topic, reply, mode, text]
                                           + ([link] if link is not None else [])
                                           + ([about] if about else [])).encode()).hexdigest()
        with self.lock, self._db() as db:
            # The lease check and durable claim share one snapshot across server instances.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT seq, digest FROM posts WHERE from_node = ? AND id = ?",
                                  (node, msg_id)).fetchone()
            if existing:
                if existing[1] != digest:
                    raise HubError(409, "this id was already used for a different message; send it under a new id")
                return self._receipt(db, existing[0], msg_id, True)
            # A sender is this node's own session or the person at its terminal, never another node's agent.
            if not isinstance(sender, str) or (not self._published(db, node, sender) and sender not in ("user", "unknown")):
                raise HubError(403, "from must be a session this node published, or user/unknown")
            if join_sender:
                # A trusted workspace caller creates membership atomically with its first topic post.
                if not target.startswith("#") or not self._published(db, node, sender):
                    raise HubError(403, "joining a topic requires a published sender and a topic target")
                db.execute("INSERT OR IGNORE INTO members (topic, node, address, joined) VALUES (?, ?, ?, ?)", (topic, node, sender, time.time()))
            recipients = self._recipients(db, target, node, sender)
            if require_online and not target.startswith("#"):
                now = time.time()
                for recipient_node, recipient_address in recipients:
                    if not self._available(db, f"{recipient_node}/{recipient_address}", now):
                        raise HubError(409, "Recipient is offline; reconnect the agent before sending a new message.")
            human = node in self.trusted_humans and sender == "user"
            linked = self._linked(db, message, f"{node}/{sender}", target)
            if not human and linked is None and about is None:
                pending = self._unanswered(db, f"{node}/{sender}", target, topic)
                if pending:
                    first = pending[0]
                    raise HubError(409, f"{target} has an open request owned by {first['owner'] or 'nobody yet'}"
                                        f" (task {first['key']}). Only its owner answers it. If you own a task, link"
                                        " your post with --task KEY --task-version N (send_message: task, task_version);"
                                        " if this is about something else, send it with --not-task"
                                        " (send_message: about=\"other\").")
            if human and reply == "expected" and mode == "push":
                # A direct request is its recipient's; a channel request is offered to every running
                # member, and the first to claim it owns it.
                channel = target.startswith("#")
                owner = None if channel else (f"{recipients[0][0]}/{recipients[0][1]}" if len(recipients) == 1 else None)
                key = tasks.key_for(node, msg_id)
                seq = self._insert(db, node, msg_id, digest, target, sender, topic, reply, mode, text,
                                   [(n, a, "owner", "push", key) if f"{n}/{a}" == owner else
                                    (n, a, "candidate", "push", key) if channel and a != "user" else
                                    (n, a, "observer", "fyi", key) for n, a in recipients])
                tasks.create(db, key, post_seq=seq, topic=topic, owner=owner, created_by=f"{node}/{sender}",
                             title=text)
            elif human:
                # A human post that asks for nothing (an announcement) informs everyone and wakes nobody.
                seq = self._insert(db, node, msg_id, digest, target, sender, topic, reply, mode, text,
                                   [(n, a, None, "fyi", None) for n, a in recipients])
            else:
                seq = self._insert(db, node, msg_id, digest, target, sender, topic, reply, mode, text,
                                   [(n, a, None, None, linked) for n, a in recipients], task=linked)
            return self._receipt(db, seq, msg_id, False)

    @staticmethod
    def _reported(item) -> tuple:
        """Validate one reported message; the values a post and its settled delivery need."""
        if not isinstance(item, dict):
            raise HubError(400, "each reported message is an object")
        msg_id, sender, target, topic = item.get("id"), item.get("from"), item.get("to"), item.get("topic")
        text, at, outcome = item.get("text"), item.get("at"), item.get("outcome")
        reply, mode = item.get("reply", "expected"), item.get("mode", "push")
        if not isinstance(msg_id, str) or not MESSAGE_ID.fullmatch(msg_id):
            raise HubError(400, "id must be 32 lowercase hex characters")
        if not all(isinstance(a, str) and LOCAL_AGENT.fullmatch(a) for a in (sender, target)):
            raise HubError(403, "a node reports only messages between its own agents (harness:id)")
        if topic is not None and (not isinstance(topic, str) or not TOPIC.fullmatch(topic)):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            raise HubError(400, f"text must be a non-empty string of at most {MAX_TEXT} characters")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise HubError(400, "text must be valid Unicode") from None
        if reply not in ("expected", "none", "inline") or mode not in MODES or outcome not in OUTCOMES:
            raise HubError(400, "reply, mode or outcome is not a known value")
        if type(at) not in (int, float) or not 0 < at < 32_503_680_000:
            raise HubError(400, "at is the original send time in epoch seconds")
        return msg_id, sender, target, topic, text, float(at), outcome, reply, mode

    def report(self, node: str, items) -> dict:
        """Record messages this node already delivered between its own agents, as history only.

        Each becomes a post with its original id, time and target, and one delivery whose outcome
        is already set, so no inbox ever hands it out again. A retry is a duplicate; the same id
        with other content is a conflict and changes nothing; an invalid item is refused alone.
        """
        if not isinstance(items, list) or not 1 <= len(items) <= REPORT_BATCH:
            raise HubError(400, f"report between 1 and {REPORT_BATCH} messages")
        results = []
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in items:
                try:
                    msg_id, sender, target, topic, text, at, outcome, reply, mode = self._reported(item)
                except HubError as error:
                    ident = item.get("id") if isinstance(item, dict) and isinstance(item.get("id"), str) else None
                    results.append({"id": ident, "status": "refused", "error": str(error)})
                    continue
                # The original time and outcome are part of the record: a changed observation is a conflict.
                digest = hashlib.sha256(json.dumps(["reported", target, sender, topic, reply, mode, text, at,
                                                    outcome]).encode()).hexdigest()
                existing = db.execute("SELECT digest FROM posts WHERE from_node = ? AND id = ?",
                                      (node, msg_id)).fetchone()
                if existing:
                    results.append({"id": msg_id, "status": "duplicate" if existing[0] == digest else "conflict"})
                    continue
                seq = db.execute("INSERT INTO posts (from_node, id, digest, target, sender, topic, reply, mode, text,"
                                 " created, reported) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                                 (node, msg_id, digest, f"{node}/{target}", sender, topic, reply, mode, text,
                                  at)).lastrowid
                db.execute("INSERT INTO deliveries (delivery, post, to_node, to_address, taken, outcome, outcome_at)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (secrets.token_hex(16), seq, node, target, at, outcome, at))
                results.append({"id": msg_id, "status": "recorded"})
        return {"results": results}

    def inbox(self, node: str, wait: float) -> list[dict]:
        """Deliveries for this node without an outcome yet, waiting up to `wait` seconds for one.

        A delivery stays here until acknowledged, so one handed out before a lost response is
        handed out again; the bridge recognises it by its delivery id.
        """
        deadline = time.monotonic() + max(0.0, min(wait, MAX_WAIT))
        while True:
            with self.lock, self._db() as db:
                self._seen(db, node)
                rows = db.execute(
                    "SELECT d.delivery, p.id, d.to_address, p.from_node, p.sender, p.topic, p.reply,"
                    " COALESCE(d.mode, p.mode), p.text, p.created, d.role, k.key, k.version, k.state, k.owner,"
                    " k.kind, k.parent, p.kind"
                    " FROM deliveries d JOIN posts p ON p.seq = d.post LEFT JOIN tasks k ON k.key = d.task"
                    " WHERE d.to_node = ? AND d.outcome IS NULL ORDER BY d.post LIMIT 50", (node,)).fetchall()
                if rows:
                    db.executemany("UPDATE deliveries SET taken = ? WHERE delivery = ?",
                                   [(time.time(), r[0]) for r in rows])
            if rows or time.monotonic() >= deadline:
                return [self._handed(node, row) for row in rows]
            time.sleep(0.25)

    @staticmethod
    def _handed(node: str, row) -> dict:
        """One inbox item. An instruction is handed out as such only to the task's current holder of an
        open task; anyone else gets it as context, whatever was recorded when it was queued."""
        d, i, a, fn, s, t, r, m, x, c, role, key, version, state, owner, kind, parent, post_kind = row
        if role in ("owner", "collaborator") and (owner != f"{node}/{a}" or state in tasks.FINISHED):
            role, m = "observer", "fyi"
        # An offer is only worth waking for while nobody holds the request.
        if role == "candidate" and (owner is not None or state in tasks.FINISHED):
            role, m = "observer", "fyi"
        # An instruction the hub issued is labelled as such, never as a message from the human or an agent.
        return {"delivery": d, "id": i, "to": a, "from": f"{fn}/{s}", "topic": t, "reply": r, "mode": m,
                "text": x, "created": c, **({"role": role} if role else {}),
                **({"system": True} if post_kind == "system" else {}),
                **({"task": {"key": key, "version": version, "state": state, "owner": owner, "kind": kind,
                             "parent": parent}} if key else {})}

    def ack(self, node: str, delivery: str, outcome: str) -> dict:
        """Record the outcome of one of this node's deliveries; the first outcome reported stands."""
        if outcome not in OUTCOMES:
            raise HubError(400, f"outcome must be one of {', '.join(OUTCOMES)}")
        with self.lock, self._db() as db:
            row = db.execute("SELECT outcome FROM deliveries WHERE delivery = ? AND to_node = ?",
                             (delivery, node)).fetchone()
            if row is None:
                raise HubError(404, "no such delivery for this node")
            db.execute("UPDATE deliveries SET outcome = ?, outcome_at = ? WHERE delivery = ? AND outcome IS NULL",
                       (outcome, time.time(), delivery))
        return {"delivery": delivery, "outcome": row[0] or outcome}

    def status(self, node: str, msg_id: str) -> dict:
        """The outcome per recipient of a message this node sent; other nodes cannot read it."""
        with self._db() as db:
            post = db.execute("SELECT seq FROM posts WHERE from_node = ? AND id = ?", (node, msg_id)).fetchone()
            if post is None:
                raise HubError(404, "no such message from this node")
            rows = db.execute("SELECT delivery, to_node, to_address, outcome, outcome_at FROM deliveries"
                              " WHERE post = ? ORDER BY rowid", (post[0],)).fetchall()
        return {"id": msg_id, "recipients": [{"to": f"{n}/{a}", "delivery": d, "outcome": o or "stored",
                                              "outcome_at": at} for d, n, a, o, at in rows]}

    def join(self, node: str, topic: str, address: str) -> dict:
        """Add one of this node's sessions to a topic; only that node can add its own sessions."""
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        with self.lock, self._db() as db:
            if not self._published(db, node, address):
                raise HubError(403, "only a session this node published can join")
            db.execute("INSERT OR IGNORE INTO members (topic, node, address, joined) VALUES (?, ?, ?, ?)", (topic, node, address, time.time()))
        return {"topic": topic, "member": f"{node}/{address}"}

    def leave(self, node: str, topic: str, address: str) -> dict:
        with self.lock, self._db() as db:
            removed = db.execute("DELETE FROM members WHERE topic = ? AND node = ? AND address = ?",
                                 (topic, node, address)).rowcount
        return {"topic": topic, "member": f"{node}/{address}", "removed": bool(removed)}

    def topics(self) -> list[dict]:
        """Each channel: its current members, how many run now, and every session that posted in it."""
        now = time.time()
        with self._db() as db:
            members = db.execute("SELECT m.topic, m.node, m.address FROM members m WHERE m.address = 'user' OR EXISTS"
                                 " (SELECT 1 FROM sessions s WHERE s.node = m.node AND s.address = m.address)"
                                 " ORDER BY m.topic, m.joined").fetchall()
            last = dict(db.execute("SELECT topic, MAX(created) FROM posts WHERE topic IS NOT NULL GROUP BY topic"))
            authors = db.execute("SELECT topic, from_node || '/' || sender, MAX(created) FROM posts WHERE topic IS NOT NULL"
                                 " AND sender != 'user' AND IFNULL(kind, '') != 'system' GROUP BY topic, from_node, sender"
                                 " ORDER BY topic, MAX(created) DESC").fetchall()
            running = {route for route in {f"{n}/{a}" for _, n, a in members if a != "user"}
                       if self._available(db, route, now)}
        listing: dict[str, dict] = {}

        def entry(topic):
            return listing.setdefault(topic, {"topic": topic, "members": [], "running": 0, "participants": [],
                                              "last": last.get(topic)})
        for topic, node, address in members:
            route = f"{node}/{address}"
            entry(topic)["members"].append(route)
            entry(topic)["running"] += route in running
        for topic, route, _ in authors:
            entry(topic)["participants"].append(route)
        # A topic whose members all left, or that only reported history names, is still readable.
        for topic in sorted(set(last) - set(listing)):
            entry(topic)
        return list(listing.values())

    # ── tasks ────────────────────────────────────────────────────────────────────────────────────────

    def _actor(self, db, node: str, address: object) -> str:
        """An agent acting on a task is a session this node published; the node vouches for it."""
        if not isinstance(address, str) or not LOCAL_AGENT.fullmatch(address) or not self._published(db, node, address):
            raise HubError(403, "actor must be a session this node published (harness:id)")
        return f"{node}/{address}"

    def _task_call(self, work):
        try:
            with self.lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                return work(db)
        except tasks.TaskError as error:
            raise TaskRefused(error) from None

    def task(self, key: object) -> dict:
        def work(db):
            task = tasks.require(db, tasks.check_key(key))
            return {**task, "children": tasks.children(db, task["key"]), "events": tasks.events(db, task["key"])}
        return self._task_call(work)

    def task_list(self, topic: str | None = None, owner: str | None = None, open_only: bool = False) -> list[dict]:
        with self._db() as db:
            return tasks.listing(db, topic=topic, owner=owner, open_only=open_only)

    def claim(self, node: str, body: dict) -> dict:
        """Take an unowned task; the claimer's delivery of the request becomes the owner's."""
        def work(db):
            actor = self._actor(db, node, body.get("actor"))
            before = tasks.require(db, tasks.check_key(body.get("key")))
            task = tasks.claim(db, before["key"], actor, body.get("version"))
            if task["version"] != before["version"]:
                db.execute("UPDATE deliveries SET role = 'owner' WHERE task = ? AND to_node = ? AND to_address = ?"
                           " AND role IN ('observer', 'candidate')", (task["key"], node, body.get("actor")))
                # The claimer only had the request as context; it now gets the owner's instructions.
                original = db.execute("SELECT text FROM posts WHERE seq = ?", (task["post"],)).fetchone()
                self._notify(db, task, task["created_by"], f"You claimed task {task['key']} (version {task['version']})."
                             f"\n{original[0] if original else task['title']}", "owner")
            return task
        return self._task_call(work)

    def update_task(self, node: str, body: dict) -> dict:
        def work(db):
            task = tasks.update(db, body.get("key"), self._actor(db, node, body.get("actor")), body.get("version"),
                                body.get("state"), body.get("note"))
            if task["state"] in tasks.FINISHED:
                # A finished task gives nobody an instruction still waiting to be delivered.
                self._retire(db, task["key"])
            return task
        return self._task_call(work)

    def delegate(self, node: str, body: dict) -> dict:
        """Grant a child task and wake its collaborator with it; a retry finds the same child."""
        def work(db):
            actor = self._actor(db, node, body.get("actor"))
            to = body.get("to")
            route = self._resolve(db, to) if isinstance(to, str) and tasks.LEAD.fullmatch(to) else None
            if route is None:
                raise tasks.TaskError(404, f"{to!r} is not a published session; use @name or NODE/harness:id")
            child, created = tasks.delegate(db, body.get("key"), actor, body.get("version"), route,
                                            body.get("kind"), body.get("note"), body.get("round", 1))
            if created:
                self._notify(db, child, actor, f"Child task {child['key']} ({child['kind']}) from {actor}:\n"
                             f"{child['note'] or '(no note)'}\nRequest: {child['title']}\n"
                             "Work only this part and report to the task owner.", "collaborator")
            return child
        return self._task_call(work)

    def _notify(self, db, task: dict, sender_route: str, text: str, role: str) -> None:
        """Wake a task's owner with its assignment; an internal post that never creates a task itself."""
        node, _, sender = sender_route.partition("/")
        owner_node, _, owner = task["owner"].partition("/")
        msg_id = hashlib.sha256(f"{task['key']}@{task['version']}:{role}".encode()).hexdigest()[:32]
        if db.execute("SELECT 1 FROM posts WHERE from_node = ? AND id = ?", (node, msg_id)).fetchone():
            return
        digest = hashlib.sha256(json.dumps(["task", task["key"], task["version"], role]).encode()).hexdigest()
        self._insert(db, node, msg_id, digest, task["owner"], sender, task["topic"], "expected", "push", text,
                     [(owner_node, owner, role, "push", task["key"])], task=task["key"], kind="system")

    def assign(self, key: object, to: str | None, authority: str, version: object, note: object = None) -> dict:
        """The human gives a task to an agent, or unassigns it. Not exposed to nodes.

        `version` is the one the human saw, so a delayed reassignment never overwrites a newer
        one. The new owner is woken with the request even if it had only received it as
        context; the former owner gets a notice that the task left it.
        """
        def work(db):
            route = None
            if to is not None:
                route = self._resolve(db, tasks.check_lead(to))
                if route is None:
                    raise tasks.TaskError(404, f"{to} is not a published session")
            before = tasks.require(db, tasks.check_key(key))
            task = tasks.transfer(db, before["key"], authority, route, version, note)
            if task["version"] == before["version"]:
                return task
            self._retire(db, task["key"])
            original = db.execute("SELECT text FROM posts WHERE seq = ?", (task["post"],)).fetchone()
            request = original[0] if original else task["title"]
            if route is not None:
                self._notify(db, task, authority, f"Assigned to you: task {task['key']} (version {task['version']}).\n"
                             f"{request}", "owner")
            if before["owner"]:
                old_node, _, old = before["owner"].partition("/")
                node, _, sender = authority.partition("/")
                msg_id = hashlib.sha256(f"{task['key']}@{task['version']}:released".encode()).hexdigest()[:32]
                if not db.execute("SELECT 1 FROM posts WHERE from_node = ? AND id = ?", (node, msg_id)).fetchone():
                    self._insert(db, node, msg_id, msg_id, before["owner"], sender, task["topic"], "none", "fyi",
                                 f"Task {task['key']} is no longer yours ({'now ' + route if route else 'unassigned'})."
                                 " Stop working on it.", [(old_node, old, "observer", "fyi", task["key"])],
                                 task=task["key"], kind="system")
            return task
        return self._task_call(work)

    def _retire(self, db, key: str) -> None:
        """Make an owner's or collaborator's still-undelivered instructions for this task context only.

        Called before a new owner is notified and when a task finishes, so an instruction from an
        earlier ownership never reaches an agent as "you own this". Plain linked posts keep their mode.
        """
        db.execute("UPDATE deliveries SET role = 'observer', mode = 'fyi' WHERE task = ? AND outcome IS NULL"
                   " AND role IN ('owner', 'collaborator')", (key,))

    def adopt_trusted_human(self, node: str) -> None:
        """Trust this node's "user" as the human, and stop old queued human broadcasts from becoming work.

        Human posts queued before tasks existed are delivered as context (fyi), not as fresh
        requests: they predate ownership, so nobody owns them. History is kept as it is.
        """
        self.trusted_humans.add(node)
        with self.lock, self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS hub_migrations (name TEXT PRIMARY KEY, at REAL NOT NULL)")
            name = f"human-tasks:{node}"
            if db.execute("INSERT OR IGNORE INTO hub_migrations VALUES (?, ?)", (name, time.time())).rowcount:
                db.execute("UPDATE deliveries SET mode = 'fyi', role = 'observer' WHERE outcome IS NULL AND task IS NULL"
                           " AND post IN (SELECT seq FROM posts WHERE from_node = ? AND sender = 'user')", (node,))

    def history(self, topic: str, after: int = 0, limit: int = 100) -> list[dict]:
        """A topic's posts, oldest first, after the row whose `cursor` is `after`.

        The cursor is the hub's own post sequence: client ids are unique only per sending node.
        """
        with self._db() as db:
            rows = db.execute("SELECT p.seq, p.id, p.from_node, p.sender, p.target, p.reply, p.mode, p.text, p.created,"
                              " p.reported, COALESCE(p.task, k.key)"
                              " FROM posts p LEFT JOIN tasks k ON k.post_seq = p.seq AND k.parent IS NULL"
                              # The hub's own instructions reach their recipient through its inbox only;
                              # nobody wrote them, so a topic's history never shows them.
                              " WHERE p.topic = ? AND p.seq > ? AND p.kind IS NOT 'system' ORDER BY p.seq LIMIT ?",
                              (topic, after, max(1, min(limit, 500)))).fetchall()
        # `to` is the original target: "#topic" for a post to everyone, a route for a tagged direct message.
        # `task` is the request a post made or answers; replaying history never creates or wakes work.
        return [{"cursor": str(q), "id": i, "from": f"{fn}/{s}", "to": t, "reply": r, "mode": m, "text": x,
                 "created": c, "reported": bool(rep), **({"task": k} if k else {})}
                for q, i, fn, s, t, r, m, x, c, rep, k in rows]


def application(hub: Hub):
    """The hub's HTTP API (JSON over /v1), authenticated by a per-node bearer token."""
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def handle(request: Request, work):
        try:
            node = await run_in_threadpool(hub.authenticated_node, request)
            body = {}
            if request.method in ("POST", "PUT"):
                body = json.loads(await bounded_body(request) or b"{}")
                if not isinstance(body, dict):
                    raise HubError(400, "the request body must be a JSON object")
            return JSONResponse(await run_in_threadpool(work, node, body, request))
        except HubError as error:
            # A refused task action says who holds the task now, so a lost receipt is never guessed.
            current = getattr(error, "task", None)
            return JSONResponse({"error": str(error), **({"task": current} if current else {})},
                                status_code=error.status)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    async def bounded_body(request) -> bytes:
        """The request body, refused as soon as it would exceed MAX_BODY, whatever Content-Length says."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            raise HubError(413, "request too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise HubError(413, "request too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def wait_param(request) -> float:
        try:
            return float(request.query_params.get("wait", "0"))
        except ValueError:
            return 0.0

    async def sessions(request):
        if request.method == "PUT":
            return await handle(request, lambda node, body, _: {"published": hub.publish(node, body.get("sessions"))})
        return await handle(request, lambda node, body, _: {"sessions": hub.directory()})

    async def messages(request):
        return await handle(request, lambda node, body, _: hub.send(node, body))

    async def status(request):
        return await handle(request, lambda node, body, req: hub.status(node, req.path_params["msg_id"]))

    async def inbox(request):
        return await handle(request, lambda node, body, req: {"messages": hub.inbox(node, wait_param(req))})

    async def acks(request):
        return await handle(request, lambda node, body, _: hub.ack(node, str(body.get("delivery")),
                                                                    str(body.get("outcome"))))

    async def topics(request):
        return await handle(request, lambda node, body, _: {"topics": hub.topics()})

    async def membership(request):
        def work(node, body, req):
            topic, address = req.path_params["topic"], req.path_params["address"]
            return hub.join(node, topic, address) if req.method == "PUT" else hub.leave(node, topic, address)
        return await handle(request, work)

    async def history(request):
        def work(node, body, req):
            try:
                limit = int(req.query_params.get("limit", "100"))
            except ValueError:
                limit = 100
            after = req.query_params.get("after", "0")
            if not after.isdigit():
                raise HubError(400, "after is the cursor of a history row")
            # SQLite integers stop at 2**63 - 1; a cursor past every post simply has nothing after it.
            return {"messages": hub.history(req.path_params["topic"], min(int(after), 2**63 - 1), limit)}
        return await handle(request, work)

    async def report(request):
        return await handle(request, lambda node, body, _: hub.report(node, body.get("messages")))

    async def whoami(request):
        return await handle(request, lambda node, body, _: {"node": node})

    async def task_list(request):
        def work(node, body, req):
            params = req.query_params
            if params.get("key"):
                return {"task": hub.task(params["key"])}
            return {"tasks": hub.task_list(params.get("topic") or None, params.get("owner") or None,
                                           params.get("open") == "1")}
        return await handle(request, work)

    def task_action(action):
        async def endpoint(request):
            return await handle(request, lambda node, body, _: {"task": action(node, body)})
        return endpoint

    return Starlette(routes=[
        Route("/v1/whoami", whoami, methods=["GET"]),
        Route("/v1/sessions", sessions, methods=["GET", "PUT"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/history", report, methods=["POST"]),
        Route("/v1/messages/{msg_id}", status, methods=["GET"]),
        Route("/v1/inbox", inbox, methods=["GET"]),
        Route("/v1/acks", acks, methods=["POST"]),
        Route("/v1/topics", topics, methods=["GET"]),
        Route("/v1/topics/{topic}/members/{address:path}", membership, methods=["PUT", "DELETE"]),
        Route("/v1/topics/{topic}/messages", history, methods=["GET"]),
        # Agents act on tasks they hold; assigning belongs to the human workspace.
        Route("/v1/tasks", task_list, methods=["GET"]),
        Route("/v1/tasks/claim", task_action(hub.claim), methods=["POST"]),
        Route("/v1/tasks/update", task_action(hub.update_task), methods=["POST"]),
        Route("/v1/tasks/delegate", task_action(hub.delegate), methods=["POST"]),
    ])
