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
posts, and the session that wrote each one, stay for whoever joins later. A session works in one
channel (its first join, or the one it chooses with `work`) and only watches the others: a post
wakes the channel's working members, and reaches watchers as an fyi, so a session busy with one
channel is never pulled into another. Archived channels are hidden from the human's list and
stay readable by everyone.

A post from a trusted human (a node in `trusted_humans`, sending as "user") wakes every running
member of its channel. Each session acks it (`acknowledge`), so the human sees who read it, and
the sessions agree in the channel who does what. A human post that finds no running session is
staffed at once: a spawn order to a connected bridge that offers to start sessions, or, when
none takes it in time, a cloud worker that the control service starts (`staffing`, keyed by
the post's NODE:id).
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

from . import summary

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
# Images a human attaches to a message: stored on the hub, fetched by the receiving bridge and the workspace.
ATTACHMENT_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
ATTACHMENT_LIMIT = 10_000_000
ATTACHMENTS_PER_MESSAGE = 4
ATTACHMENT_ID = re.compile(r"[0-9a-f]{32}")
MAX_TEXT = 200_000
# A JSON-escaped text can take up to six bytes per character, plus the envelope fields.
MAX_BODY = MAX_TEXT * 6 + 4096
MAX_WAIT = 30.0
# A node that has not published or polled for this long is listed as offline.
LEASE_SECONDS = 60.0
# A member whose session has not been published for this long leaves its topics; a bridge
# restart is shorter, so a running session keeps its channels.
MEMBER_GRACE_SECONDS = 600.0
# A spawn order nobody has turned into a claim by then goes to the next spawner, else the cloud.
SPAWN_FALLBACK_SECONDS = 90.0
# Spawn orders one request gets at most: every connected spawner first, then the same ones again,
# so a start that failed by accident is retried and a machine that always fails starts no loop.
SPAWN_ATTEMPTS = 3
# Only requests created or released this recently are staffed, so old unowned ones never start sessions.
STAFF_WINDOW_SECONDS = 3600.0
# A request the cloud could not take goes back to it after this long, within the staffing window.
UNAVAILABLE_RETRY_SECONDS = 30.0
SPAWN_HARNESSES = ("claude", "codex", "omp")
# The apps a published session can name beside its harness.
PRODUCTS = ("cursor-cli", "cursor-ide")
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
CREATE TABLE IF NOT EXISTS spawners (node TEXT PRIMARY KEY, harness TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS archived_topics (topic TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS post_acks (post INTEGER NOT NULL, node TEXT NOT NULL, address TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY (post, node, address));
CREATE TABLE IF NOT EXISTS staffing (task TEXT NOT NULL, version INTEGER NOT NULL, route TEXT NOT NULL,
    state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY (task, version));
CREATE TABLE IF NOT EXISTS topic_summaries (topic TEXT NOT NULL, lo INTEGER NOT NULL, hi INTEGER NOT NULL,
    text TEXT NOT NULL, author TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (topic, lo, hi));
CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, node TEXT NOT NULL, media_type TEXT NOT NULL,
    size INTEGER NOT NULL, body BLOB NOT NULL, created REAL NOT NULL);
"""
GENERAL = "general"


class HubError(Exception):
    """A request the hub refuses, with the HTTP status to answer it with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
        # Nodes whose "user" is an authenticated human (the workspace): its posts wake every running session.
        self.trusted_humans: set[str] = set()
        # Spawners being put to sleep: their offer is ignored, so no spawn order reaches a machine that is stopping.
        self.paused_spawners: set[str] = set()
        with self._db() as db:
            db.executescript(SCHEMA)
            posts = {row[1] for row in db.execute("PRAGMA table_info(posts)")}
            # Posts a node reported after delivering them itself; they never had a hub delivery to take.
            if "reported" not in posts:
                db.execute("ALTER TABLE posts ADD COLUMN reported INTEGER NOT NULL DEFAULT 0")
            # "system" for an instruction the hub issued itself, which no human or agent wrote.
            if "kind" not in posts:
                db.execute("ALTER TABLE posts ADD COLUMN kind TEXT")
            # JSON list of the images attached to the post: id, media_type and size of each.
            if "attachments" not in posts:
                db.execute("ALTER TABLE posts ADD COLUMN attachments TEXT")
            members = {row[1] for row in db.execute("PRAGMA table_info(members)")}
            # When the member's session was last published; membership ends a grace period after it.
            if "seen" not in members:
                db.execute("ALTER TABLE members ADD COLUMN seen REAL")
            # 1 for the channel the session works in; memberships from before only watch.
            if "work" not in members:
                db.execute("ALTER TABLE members ADD COLUMN work INTEGER NOT NULL DEFAULT 0")
            # Higher first: a machine the user does not pay for outranks a cloud box.
            if "priority" not in {row[1] for row in db.execute("PRAGMA table_info(spawners)")}:
                db.execute("ALTER TABLE spawners ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            # Space-separated spawner nodes already ordered to start a session for this request.
            if "tried" not in {row[1] for row in db.execute("PRAGMA table_info(staffing)")}:
                db.execute("ALTER TABLE staffing ADD COLUMN tried TEXT NOT NULL DEFAULT ''")
            sessions = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            # Which app of the harness the session runs in, from PRODUCTS, or empty.
            if "product" not in sessions:
                db.execute("ALTER TABLE sessions ADD COLUMN product TEXT NOT NULL DEFAULT ''")
            deliveries = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            # A delivery can wake its recipient while the same post only informs the others.
            for column in ("mode",):
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

    def publish(self, node: str, sessions, spawn: object = None, priority: object = 0) -> int:
        """Replace this node's published sessions and renew its lease; a name already taken is refused.

        `spawn` is the harness this node's bridge starts a session with when the hub asks it to,
        or None when it starts none; spawners with a higher `priority` are asked first.
        """
        if not isinstance(sessions, list):
            raise HubError(400, "sessions must be a list")
        if spawn is not None and spawn not in SPAWN_HARNESSES:
            raise HubError(400, f"spawn is one of {', '.join(SPAWN_HARNESSES)}, or absent")
        if priority is None:
            priority = 0
        if type(priority) is not int or not -1000 <= priority <= 1000:
            raise HubError(400, "spawn_priority is an integer between -1000 and 1000")
        rows, names, addresses = [], set(), set()
        for item in sessions:
            if not isinstance(item, dict) or not isinstance(item.get("address"), str) or "/" in item["address"]:
                raise HubError(400, "each session needs a local address like claude:<id>")
            if item["address"] in addresses:
                raise HubError(400, f"{item['address']} is listed twice")
            addresses.add(item["address"])
            name = item.get("name")
            if name is not None and (not isinstance(name, str) or not NODE.fullmatch(name)):
                raise HubError(400, f"a name is lowercase letters, digits and dashes: {name!r}")
            if name is not None:
                if name in names:
                    raise HubError(409, f"@{name} is given to two sessions")
                names.add(name)
            product = item.get("product") or ""
            if product and product not in PRODUCTS:
                raise HubError(400, f"product is one of {', '.join(PRODUCTS)}, or absent")
            rows.append((node, item["address"], name, str(item.get("title") or ""), str(item.get("cwd") or ""),
                         str(item.get("status") or ""), time.time(), product))
        with self.lock, self._db() as db:
            for name in names:
                if db.execute("SELECT 1 FROM sessions WHERE name = ? AND node != ?", (name, node)).fetchone():
                    raise HubError(409, f"@{name} is already used on another node")
            db.execute("DELETE FROM sessions WHERE node = ?", (node,))
            db.executemany("INSERT INTO sessions (node, address, name, title, cwd, status, updated, product)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            now = time.time()
            db.executemany("INSERT OR REPLACE INTO session_seen VALUES (?, ?, ?)", [(node, r[1], now) for r in rows])
            db.executemany("UPDATE members SET seen = ? WHERE node = ? AND address = ?", [(now, node, r[1]) for r in rows])
            if spawn is None or node in self.paused_spawners:
                db.execute("DELETE FROM spawners WHERE node = ?", (node,))
            else:
                db.execute("INSERT OR REPLACE INTO spawners (node, harness, updated, priority) VALUES (?, ?, ?, ?)",
                           (node, spawn, now, priority))
            self._seen(db, node)
            # A clock read before _seen would put this node's lease in the future and count its sessions offline.
            self._sweep(db, time.time())
        return len(rows)

    def sweep(self) -> list[dict]:
        """End stale memberships and staff waiting human posts."""
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._sweep(db, time.time())

    def _sweep(self, db, now: float) -> list[dict]:
        """End stale memberships and staff waiting human posts; returns nothing worth reporting."""
        # A session last published before the grace period, on any node, has left its topics.
        db.execute("DELETE FROM members WHERE address != 'user' AND IFNULL(seen, joined) < ? AND NOT EXISTS ("
                   "SELECT 1 FROM sessions s JOIN nodes n ON n.node = s.node WHERE s.node = members.node"
                   " AND s.address = members.address AND n.seen >= ?)",
                   (now - MEMBER_GRACE_SECONDS, now - LEASE_SECONDS))
        self._staff_waiting(db, now)
        return []

    # ── staffing: from no running session to one ─────────────────────────────────────────────────

    def _running(self, db, topic: str | None, now: float) -> bool:
        """Whether a session is running in this channel to read a human post."""
        if not topic:
            return False
        return any(a != "user" and work and self._available(db, f"{n}/{a}", now)
                   for n, a, work in self._members(db, topic))

    def _human_post(self, db, seq: int) -> dict | None:
        row = db.execute("SELECT from_node, id, sender, topic, text, created FROM posts WHERE seq = ?",
                         (seq,)).fetchone()
        if row is None:
            return None
        node, ident, sender, topic, text, created = row
        return {"key": f"{node}:{ident}", "seq": seq, "topic": topic, "text": text, "created": created,
                "created_by": f"{node}/{sender}"}

    def _spawners(self, db, now: float, tried=()) -> list[tuple[str, str]]:
        """Connected spawners not yet tried, the preferred first."""
        # Only a bridge publication renews `updated`; MCP calls on that node keep `nodes.seen` fresh without a bridge.
        rows = db.execute("SELECT s.node, s.harness FROM spawners s JOIN nodes n ON n.node = s.node"
                          " WHERE n.seen >= ? AND s.updated >= ? ORDER BY s.priority DESC, s.updated DESC",
                          (now - LEASE_SECONDS, now - LEASE_SECONDS)).fetchall()
        return [(node, harness) for node, harness in rows if node not in tried]

    def _next_spawner(self, db, now: float, tried: list[str], refused=()) -> tuple[str, str] | None:
        """A connected spawner not yet tried, the preferred first; once all were, any that did not refuse,
        within SPAWN_ATTEMPTS orders."""
        spawners = self._spawners(db, now, tried)
        if not spawners and len(tried) < SPAWN_ATTEMPTS:
            spawners = [s for s in self._spawners(db, now) if s[0] not in refused]
        return spawners[0] if spawners else None

    def _refusers(self, db, node: str, tried: list[str], spawn_ids: list[str]) -> set[str]:
        """The spawners that refused this request's orders."""
        if not spawn_ids:
            return set()
        refused = {row[0] for row in db.execute(
            f"SELECT p.id FROM posts p JOIN deliveries d ON d.post = p.seq WHERE p.from_node = ? AND p.kind = 'system'"
            f" AND d.outcome = 'refused' AND p.id IN ({','.join('?' * len(spawn_ids))})", (node, *spawn_ids))}
        return {n for n, ident in zip(tried, spawn_ids) if ident in refused}

    @staticmethod
    def _spawn_id(key: str, earlier: list[str], node: str) -> str:
        """The id of the order sent to `node` after orders to `earlier`; each order of a request has its own.

        The first order and each node's first fallback order keep the ids they always had.
        """
        suffix = "" if not earlier else f":{node}" if node not in earlier else f":{node}:{len(earlier)}"
        return hashlib.sha256(f"{key}:spawn{suffix}".encode()).hexdigest()[:32]

    def _order_spawn(self, db, post: dict, node: str, harness: str, earlier: list[str]) -> str:
        """Queue a spawn order for this post to one spawner; returns its route."""
        route = f"{node}/spawn:{harness}"
        from_node, _, sender = post["created_by"].partition("/")
        digest = hashlib.sha256(json.dumps(["spawn", post["key"], node, len(earlier)]).encode()).hexdigest()
        self._insert(db, from_node, self._spawn_id(post["key"], earlier, node), digest, route, sender, post["topic"],
                     "expected", "push", self.spawn_prompt(post, self._predecessors(db, post["topic"])), [(node, f"spawn:{harness}", None)], kind="system")
        return route

    def _staff(self, db, post: dict, now: float) -> dict:
        """Start a session for a human post nobody runs to read: a spawn order to a connected spawner, else the cloud."""
        row = db.execute("SELECT route, state FROM staffing WHERE task = ? AND version = 1", (post["key"],)).fetchone()
        if row:
            return {"route": row[0], "state": row[1]}
        spawner = self._next_spawner(db, now, [])
        if spawner is None:
            db.execute("INSERT INTO staffing (task, version, route, state, detail, created, updated)"
                       " VALUES (?, 1, 'cloud', 'cloud', '', ?, ?)", (post["key"], now, now))
            return {"route": "cloud", "state": "cloud"}
        node, harness = spawner
        route = self._order_spawn(db, post, node, harness, [])
        db.execute("INSERT INTO staffing (task, version, route, state, detail, created, updated, tried)"
                   " VALUES (?, 1, ?, 'node', '', ?, ?, ?)", (post["key"], route, now, now, node))
        return {"route": route, "state": "node"}

    def _predecessors(self, db, topic: str | None, limit: int = 3) -> list[str]:
        """Agent sessions that posted in this channel, the most recent first: the work a new session takes over."""
        if not topic:
            return []
        rows = db.execute("SELECT from_node, sender FROM posts WHERE topic = ? AND sender != 'user' AND kind IS NULL"
                          " GROUP BY from_node, sender ORDER BY MAX(seq) DESC LIMIT ?", (topic, limit)).fetchall()
        return [f"{node}/{sender}" for node, sender in rows if ":" in sender]

    @staticmethod
    def spawn_prompt(post: dict, predecessors: list[str] = ()) -> str:
        """The first prompt of a session started for a human post: take over, join, ack, sync, answer."""
        ident = post["key"].partition(":")[2]
        takeover = ""
        if predecessors:
            # Any agent can read any session from any machine: that is what lets this one continue the work.
            takeover = ("Sessions that worked in this channel before, the most recent first (they are not running now;"
                        " their machine may be asleep or gone):\n" + "".join(f"- {a}\n" for a in predecessors) +
                        "Take over their work: before anything else, read the most recent one with read_conversation"
                        " (query=its address, namespace=\"local\"; around=a message id to zoom in), and the others as"
                        " needed, then continue exactly where it stopped. Its pushed branches are on the remote; work it"
                        " did not push is lost, so redo it.\n\n")
        return (f"{post['created_by']} posted in #{post['topic']} and no session was running there, so this session"
                f" was started for it.\n\nMessage:\n{post['text']}\n\n{takeover}"
                f"1. Join #{post['topic']} with the join_topic tool (work=true): it is your work channel, and"
                " only its posts wake you.\n"
                f"2. Ack the message with the ack tool (message_id=\"{ident}\").\n"
                f"3. Work it and answer in #{post['topic']}. Other sessions may join: before starting a piece of work,"
                " say in the channel what you take, and leave anything already taken to whoever took it. To dig into"
                " an earlier session, read it with read_conversation using a message's sender address.\n"
                "4. This machine can disappear at any time: `git pull` every repository before working in it, and"
                " commit and push every change as soon as it is made. AGENTS.md in this folder has the details.")

    def _staff_waiting(self, db, now: float) -> None:
        """Staff recent human posts nobody acked or runs to read, pass stalled spawn orders on to the next spawner, close the rest."""
        humans = tuple(f"{node}" for node in self.trusted_humans)
        if humans:
            rows = db.execute(
                f"SELECT p.seq FROM posts p WHERE p.from_node IN ({','.join('?' * len(humans))}) AND p.sender = 'user'"
                " AND p.target LIKE '#%' AND p.reply = 'expected' AND p.mode = 'push' AND p.kind IS NULL"
                " AND p.created >= ? AND NOT EXISTS (SELECT 1 FROM post_acks a WHERE a.post = p.seq)",
                (*humans, now - STAFF_WINDOW_SECONDS)).fetchall()
            for (seq,) in rows:
                post = self._human_post(db, seq)
                if not self._running(db, post["topic"], now):
                    self._staff(db, post, now)
        for key, route, state, updated, tried in db.execute(
                "SELECT task, route, state, updated, tried FROM staffing WHERE state IN ('node', 'cloud', 'submitted', 'unavailable')"
                ).fetchall():
            node, _, ident = key.partition(":")
            # Rows staffed before `tried` existed name their only spawner in the route.
            tried = tried.split() or ([route.partition("/")[0]] if route != "cloud" else [])
            spawn_ids = [self._spawn_id(key, tried[:i], n) for i, n in enumerate(tried)]
            seq = db.execute("SELECT seq, topic FROM posts WHERE from_node = ? AND id = ?", (node, ident)).fetchone()
            acked = seq is not None and db.execute("SELECT 1 FROM post_acks WHERE post = ?", (seq[0],)).fetchone()
            if seq is None or acked:
                db.execute("UPDATE staffing SET state = 'done', updated = ? WHERE task = ?", (now, key))
                # A spawn order not yet handed out starts nothing once a session has read the post.
                if spawn_ids:
                    db.execute(f"UPDATE deliveries SET mode = 'fyi' WHERE outcome IS NULL AND post IN (SELECT seq FROM"
                               f" posts WHERE from_node = ? AND kind = 'system' AND id IN ({','.join('?' * len(spawn_ids))}))",
                               (node, *spawn_ids))
                continue
            if route == "cloud":
                # A spawner that connects later, such as a cloud box coming back, takes a request still waiting.
                post = self._human_post(db, seq[0])
                # A failed cloud submit, such as a worker limit, is tried again rather than left unstaffed.
                if (state == "unavailable" and updated < now - UNAVAILABLE_RETRY_SECONDS
                        and post["created"] >= now - STAFF_WINDOW_SECONDS):
                    db.execute("UPDATE staffing SET state = 'cloud', updated = ? WHERE task = ?", (now, key))
                    state = "cloud"
                if (state == "cloud" and post["created"] >= now - STAFF_WINDOW_SECONDS
                        and (spawner := self._next_spawner(db, now, tried, self._refusers(db, node, tried, spawn_ids)))):
                    spawner_node, harness = spawner
                    route = self._order_spawn(db, post, spawner_node, harness, tried)
                    db.execute("UPDATE staffing SET route = ?, state = 'node', updated = ?, tried = ? WHERE task = ?",
                               (route, now, " ".join([*tried, spawner_node]), key))
                continue
            spawner_node, _, address = route.partition("/")
            outcome = db.execute("SELECT d.outcome FROM deliveries d JOIN posts p ON p.seq = d.post WHERE p.kind = 'system'"
                                 " AND p.from_node = ? AND p.id = ? AND d.to_node = ? AND d.to_address = ?",
                                 (node, spawn_ids[-1], spawner_node, address)).fetchone()
            refused = outcome is not None and outcome[0] == "refused"
            if refused or updated < now - SPAWN_FALLBACK_SECONDS:
                why = f"{route} {'refused the spawn' if refused else 'started no session that acked in time'}"
                # An order its spawner never took starts nothing once the request has moved on.
                db.execute("UPDATE deliveries SET mode = 'fyi' WHERE outcome IS NULL AND post IN"
                           " (SELECT seq FROM posts WHERE from_node = ? AND kind = 'system' AND id = ?)",
                           (node, spawn_ids[-1]))
                spawner = self._next_spawner(db, now, tried, self._refusers(db, node, tried, spawn_ids))
                if spawner:
                    post = self._human_post(db, seq[0])
                    next_node, harness = spawner
                    next_route = self._order_spawn(db, post, next_node, harness, tried)
                    db.execute("UPDATE staffing SET route = ?, detail = ?, updated = ?, tried = ? WHERE task = ?",
                               (next_route, why, now, " ".join([*tried, next_node]), key))
                else:
                    db.execute("UPDATE staffing SET route = 'cloud', state = 'cloud', detail = ?, updated = ? WHERE task = ?",
                               (why, now, key))

    def spawner_activity(self, node: str) -> dict:
        """Whether this spawner has work (a busy session or a spawn order in flight) and whether a request waits for it."""
        now = time.time()
        with self.lock, self._db() as db:
            busy = db.execute("SELECT 1 FROM sessions s JOIN nodes n ON n.node = s.node WHERE s.node = ?"
                              " AND s.status = 'busy' AND s.updated >= ? AND n.seen >= ?",
                              (node, now - LEASE_SECONDS, now - LEASE_SECONDS)).fetchone() is not None
            busy = busy or db.execute("SELECT 1 FROM staffing WHERE state = 'node' AND route LIKE ?",
                                      (f"{node}/%",)).fetchone() is not None
            wanted = any(node not in tried.split() or len(tried.split()) < SPAWN_ATTEMPTS for (tried,) in db.execute(
                "SELECT tried FROM staffing WHERE state = 'cloud' AND created >= ?", (now - STAFF_WINDOW_SECONDS,)))
        return {"busy": busy, "wanted": wanted}

    def pause_spawner(self, node: str) -> bool:
        """Stop offering this spawner unless it has work; True when it is paused and its machine can stop."""
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if (db.execute("SELECT 1 FROM sessions WHERE node = ? AND status = 'busy' AND updated >= ?",
                           (node, now - LEASE_SECONDS)).fetchone()
                    or db.execute("SELECT 1 FROM staffing WHERE state = 'node' AND route LIKE ?", (f"{node}/%",)).fetchone()):
                return False
            self.paused_spawners.add(node)
            db.execute("DELETE FROM spawners WHERE node = ?", (node,))
        return True

    def resume_spawner(self, node: str) -> None:
        """Take this spawner's offer again from its next publication."""
        with self.lock:
            self.paused_spawners.discard(node)

    def node_seen(self, node: str) -> float | None:
        """When this node last published or polled, or None if it never did."""
        with self.lock, self._db() as db:
            row = db.execute("SELECT seen FROM nodes WHERE node = ?", (node,)).fetchone()
        return row[0] if row else None

    def staffing_due(self) -> list[dict]:
        """Human posts waiting for a cloud worker, with the first prompt that worker gets."""
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._sweep(db, time.time())
            due = []
            for (key,) in db.execute("SELECT task FROM staffing WHERE state = 'cloud' ORDER BY created").fetchall():
                node, _, ident = key.partition(":")
                seq = db.execute("SELECT seq FROM posts WHERE from_node = ? AND id = ?", (node, ident)).fetchone()
                post = self._human_post(db, seq[0])
                due.append({"task": key, "version": 1, "topic": post["topic"], "prompt": self.spawn_prompt(post, self._predecessors(db, post["topic"]))})
            return due

    def staffing_mark(self, key: str, version: int, state: str, detail: str = "") -> None:
        """Record what the control service did about a cloud staffing request."""
        if state not in ("submitted", "unavailable"):
            raise HubError(400, "a staffing request is submitted or unavailable")
        with self.lock, self._db() as db:
            db.execute("UPDATE staffing SET state = ?, detail = ?, updated = ? WHERE task = ? AND version = ?"
                       " AND state = 'cloud'", (state, detail[:500], time.time(), key, version))

    # ── acknowledgments ──────────────────────────────────────────────────────────────────────────────

    def acknowledge(self, node: str, actor: object, msg_id: object) -> dict:
        """A session says it has read a message it received; a repeat changes nothing."""
        if not isinstance(msg_id, str) or not MESSAGE_ID.fullmatch(msg_id):
            raise HubError(400, "message_id is the 32-hex id the message arrived with")
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            route = self._actor(db, node, actor)
            # A session acks what it received, or any post of a channel it joined (one started for that post).
            row = db.execute("SELECT p.seq FROM posts p WHERE p.id = ? AND p.kind IS NOT 'system' AND ("
                             "EXISTS (SELECT 1 FROM deliveries d WHERE d.post = p.seq AND d.to_node = ?"
                             " AND d.to_address = ?) OR EXISTS (SELECT 1 FROM members m WHERE m.topic = p.topic"
                             " AND p.target LIKE '#%' AND m.node = ? AND m.address = ?)) ORDER BY p.seq DESC LIMIT 1",
                             (msg_id, node, actor, node, actor)).fetchone()
            if row is None:
                raise HubError(404, "no message with this id reached you or a channel you joined")
            db.execute("INSERT OR IGNORE INTO post_acks VALUES (?, ?, ?, ?)", (row[0], node, actor, time.time()))
            return {"id": msg_id, "acked_by": route, "acks": self._acks(db, [row[0]]).get(row[0], [])}

    @staticmethod
    def _acks(db, seqs: list[int]) -> dict[int, list[str]]:
        """Who acked each of these posts, in ack order."""
        if not seqs:
            return {}
        acks: dict[int, list[str]] = {}
        for seq, route in db.execute(f"SELECT post, node || '/' || address FROM post_acks WHERE post IN"
                                     f" ({','.join('?' * len(seqs))}) ORDER BY at", seqs):
            acks.setdefault(seq, []).append(route)
        return acks

    def directory(self) -> list[dict]:
        """Every published session, with independent bridge and publication timestamps."""
        now = time.time()
        with self._db() as db:
            seen = dict(db.execute("SELECT node, seen FROM nodes"))
            rows = db.execute("SELECT node, address, name, title, cwd, status, updated, product FROM sessions "
                              "ORDER BY node, address").fetchall()
        return [{"address": f"{node}/{address}", "node": node, "local": address, "name": name, "title": title,
                 "cwd": cwd, "status": status, "product": product, "seen": seen.get(node),
                 "published_at": published, "online": _online(seen.get(node, 0.0), published, now)}
                for node, address, name, title, cwd, status, published, product in rows]

    def _published(self, db, node: str, address: str) -> bool:
        return db.execute("SELECT 1 FROM sessions WHERE node = ? AND address = ?", (node, address)).fetchone() is not None

    def _recipients(self, db, target: str, from_node: str, sender: str) -> list[tuple[str, str]]:
        """Where a message goes: one session, a named one, or every member of a topic but the sender."""
        if target.startswith("#"):
            return [(n, a) for n, a, _ in self._members(db, target[1:]) if (n, a) != (from_node, sender)]
        if target.startswith("@"):
            row = db.execute("SELECT node, address FROM sessions WHERE name = ?", (target[1:],)).fetchone()
            if row is None:
                raise HubError(404, f"no published session is named {target}")
            return [row]
        node, address = split_address(target)
        if not self._published(db, node, address):
            raise HubError(404, f"{target} is not a published session")
        return [(node, address)]

    def _members(self, db, topic: str) -> list[tuple[str, str, bool]]:
        """A channel's members whose session is still published, with whether each works there.

        The human's mailbox always counts as working: it receives every post.
        """
        rows = db.execute("SELECT m.node, m.address, m.work FROM members m WHERE m.topic = ? AND (m.address = 'user'"
                          " OR EXISTS (SELECT 1 FROM sessions s WHERE s.node = m.node AND s.address = m.address))"
                          " ORDER BY m.joined", (topic,)).fetchall()
        return [(n, a, bool(w) or a == "user") for n, a, w in rows]

    def _modes(self, db, target: str, recipients: list[tuple[str, str]], mode: str | None) -> list[tuple]:
        """Deliveries for a post: a channel wakes its working members and only informs its watchers."""
        if not target.startswith("#") or mode == "fyi":
            return [(n, a, mode) for n, a in recipients]
        working = {(n, a) for n, a, w in self._members(db, target[1:]) if w}
        return [(n, a, mode if (n, a) in working else "fyi") for n, a in recipients]

    def _receipt(self, db, seq: int, msg_id: str, duplicate: bool) -> dict:
        rows = db.execute("SELECT delivery, to_node, to_address, outcome FROM deliveries WHERE post = ? "
                          "ORDER BY rowid", (seq,)).fetchall()
        return {"id": msg_id, "duplicate": duplicate,
                "recipients": [{"to": f"{n}/{a}", "delivery": d, "status": o or "stored"} for d, n, a, o in rows]}

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
                reply: str, mode: str, text: str, deliveries, kind: str | None = None,
                attachments: list[dict] | None = None) -> int:
        """One post and its deliveries; `deliveries` is (node, address, mode) per recipient, mode None for the post's.

        `kind="system"` marks an instruction the hub itself issued (a spawn order): it is delivered
        like any post but is never shown as something the human or an agent wrote.
        """
        seq = db.execute("INSERT INTO posts (from_node, id, digest, target, sender, topic, reply, mode, text, created,"
                         " kind, attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (node, msg_id, digest, target, sender, topic, reply, mode, text, time.time(), kind,
                          json.dumps(attachments) if attachments else None)).lastrowid
        db.executemany("INSERT INTO deliveries (delivery, post, to_node, to_address, mode) VALUES (?, ?, ?, ?, ?)",
                       [(secrets.token_hex(16), seq, n, a, m) for n, a, m in deliveries])
        return seq

    def send(self, node: str, message: dict, *, join_sender: bool = False, require_online: bool = False) -> dict:
        """Store a post and one delivery per recipient; a retry of the same post returns its first receipt."""
        msg_id, sender, text = message.get("id"), message.get("from"), message.get("text")
        if not isinstance(msg_id, str) or not MESSAGE_ID.fullmatch(msg_id):
            raise HubError(400, "id must be 32 lowercase hex characters, chosen by the sender")
        attached = message.get("attachments") or []
        if (not isinstance(attached, list) or len(attached) > ATTACHMENTS_PER_MESSAGE
                or not all(isinstance(a, str) and ATTACHMENT_ID.fullmatch(a) for a in attached)):
            raise HubError(400, f"attachments is a list of at most {ATTACHMENTS_PER_MESSAGE} attachment ids")
        if not isinstance(text, str) or (not text and not attached) or len(text) > MAX_TEXT:
            raise HubError(400, f"text must be a string of at most {MAX_TEXT} characters, non-empty unless an image is attached")
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
        digest = hashlib.sha256(json.dumps([target, sender, topic, reply, mode, text, *attached]).encode()).hexdigest()
        with self.lock, self._db() as db:
            # The lease check and durable claim share one snapshot across server instances.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT seq, digest FROM posts WHERE from_node = ? AND id = ?",
                                  (node, msg_id)).fetchone()
            if existing:
                if existing[1] != digest:
                    raise HubError(409, "this id was already used for a different message; send it under a new id")
                return self._receipt(db, existing[0], msg_id, True)
            attachments = self._attachments(db, attached)
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
            if human and reply == "expected" and mode == "push":
                # Every running member reads a human post; they ack it and agree in the channel who does what.
                seq = self._insert(db, node, msg_id, digest, target, sender, topic, reply, mode, text,
                                   self._modes(db, target, recipients, None), attachments=attachments)
                # A human post brings an archived channel back to the human's list.
                if target.startswith("#"):
                    db.execute("DELETE FROM archived_topics WHERE topic = ?", (topic,))
                if target.startswith("#") and not self._running(db, topic, time.time()):
                    self._staff(db, self._human_post(db, seq), time.time())
            elif human:
                # A human post that asks for nothing (an announcement) informs everyone and wakes nobody.
                seq = self._insert(db, node, msg_id, digest, target, sender, topic, reply, mode, text,
                                   [(n, a, "fyi") for n, a in recipients], attachments=attachments)
            else:
                seq = self._insert(db, node, msg_id, digest, target, sender, topic, reply, mode, text,
                                   self._modes(db, target, recipients, None), attachments=attachments)
            receipt = self._receipt(db, seq, msg_id, False)
            if topic is not None:
                # The sender whose post completes a block writes its summary.
                request = self._pending(db, topic)
                if request:
                    receipt["summarize"] = request
            return receipt

    def _attachments(self, db, ids: list[str]) -> list[dict] | None:
        """The metadata of the attached images, in the order given; an unknown id refuses the message."""
        if not ids:
            return None
        out = []
        for ident in ids:
            row = db.execute("SELECT media_type, size FROM attachments WHERE id = ?", (ident,)).fetchone()
            if row is None:
                raise HubError(400, f"attachment {ident} was not uploaded")
            out.append({"id": ident, "media_type": row[0], "size": row[1]})
        return out

    def store_attachment(self, node: str, media_type: str, body: bytes) -> dict:
        """Keep one uploaded image; its id goes into the message that carries it."""
        if media_type not in ATTACHMENT_TYPES:
            raise HubError(415, "attach a PNG, JPEG, GIF or WebP image")
        if not body or len(body) > ATTACHMENT_LIMIT:
            raise HubError(413, f"an image is at most {ATTACHMENT_LIMIT // 1_000_000} MB")
        ident = secrets.token_hex(16)
        with self.lock, self._db() as db:
            db.execute("INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?)",
                       (ident, node, media_type, len(body), body, time.time()))
        return {"id": ident, "media_type": media_type, "size": len(body)}

    def attachment(self, ident: str) -> tuple[str, bytes]:
        """(media_type, body) of an uploaded image; any authenticated node may fetch what was sent to it."""
        if not ATTACHMENT_ID.fullmatch(ident):
            raise HubError(400, "an attachment id is 32 lowercase hex characters")
        with self._db() as db:
            row = db.execute("SELECT media_type, body FROM attachments WHERE id = ?", (ident,)).fetchone()
        if row is None:
            raise HubError(404, "no such attachment")
        return row[0], bytes(row[1])

    @staticmethod
    def _parsed(attachments) -> list[dict]:
        try:
            rows = json.loads(attachments) if attachments else []
        except ValueError:
            return []
        return rows if isinstance(rows, list) else []

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
        # The human at the node's terminal may be either end: a prompt typed into one of its sessions, or
        # the reply that session gave there.
        agent = {end: isinstance(end, str) and LOCAL_AGENT.fullmatch(end) is not None for end in (sender, target)}
        if not (agent[target] and (sender == "user" or agent[sender])) and not (agent[sender] and target == "user"):
            raise HubError(403, "a node reports only messages between its own agents (harness:id), "
                                "or between the human at its terminal and one of them")
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
                    " COALESCE(d.mode, p.mode), p.text, p.created, p.kind, p.attachments"
                    " FROM deliveries d JOIN posts p ON p.seq = d.post"
                    " WHERE d.to_node = ? AND d.outcome IS NULL ORDER BY d.post LIMIT 50", (node,)).fetchall()
                if rows:
                    db.executemany("UPDATE deliveries SET taken = ? WHERE delivery = ?",
                                   [(time.time(), r[0]) for r in rows])
            if rows or time.monotonic() >= deadline:
                return [self._handed(row) for row in rows]
            time.sleep(0.25)

    @classmethod
    def _handed(cls, row) -> dict:
        """One inbox item; an instruction the hub issued is labelled as such, never as the human's or an agent's."""
        d, i, a, fn, s, t, r, m, x, c, post_kind, attachments = row
        return {"delivery": d, "id": i, "to": a, "from": f"{fn}/{s}", "topic": t, "reply": r, "mode": m,
                "text": x, "created": c, **({"system": True} if post_kind == "system" else {}),
                **({"attachments": cls._parsed(attachments)} if attachments else {})}

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

    def join(self, node: str, topic: str, address: str, work: bool | None = None) -> dict:
        """Add one of this node's sessions to a topic; only that node can add its own sessions.

        `work=True` makes it the session's work channel (its previous one is watched from then on),
        `work=False` only watches, and None works there when the session works nowhere yet.
        """
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._published(db, node, address):
                raise HubError(403, "only a session this node published can join")
            db.execute("INSERT OR IGNORE INTO members (topic, node, address, joined) VALUES (?, ?, ?, ?)",
                       (topic, node, address, time.time()))
            elsewhere = db.execute("SELECT topic FROM members WHERE node = ? AND address = ? AND work = 1"
                                   " AND topic != ?", (node, address, topic)).fetchone()
            if work or (work is None and elsewhere is None):
                db.execute("UPDATE members SET work = (topic = ?) WHERE node = ? AND address = ?", (topic, node, address))
            elif work is False:
                db.execute("UPDATE members SET work = 0 WHERE topic = ? AND node = ? AND address = ?",
                           (topic, node, address))
            working = db.execute("SELECT work FROM members WHERE topic = ? AND node = ? AND address = ?",
                                 (topic, node, address)).fetchone()[0]
        return {"topic": topic, "member": f"{node}/{address}", "work": bool(working)}

    def archive(self, topic: str, archived: bool) -> dict:
        """Hide a channel from the human's list, or bring it back; agents still list and read it."""
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        with self.lock, self._db() as db:
            if archived:
                db.execute("INSERT OR REPLACE INTO archived_topics VALUES (?, ?)", (topic, time.time()))
            else:
                db.execute("DELETE FROM archived_topics WHERE topic = ?", (topic,))
        return {"topic": topic, "archived": archived}

    def stop(self, from_node: str, route: str) -> dict:
        """Order the bridge of `route`'s machine to end that session; returns the order's id.

        The order is a system post addressed to `stop:<address>` on that machine, so only its
        bridge acts on it and it never shows as a message anyone wrote.
        """
        node, address = split_address(route)
        if address == "user":
            raise HubError(400, "only an agent session can be stopped")
        msg_id = secrets.token_hex(16)
        target = f"{node}/stop:{address}"
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._available(db, route, time.time()):
                raise HubError(409, "This agent is offline; its machine cannot receive a stop order.")
            digest = hashlib.sha256(json.dumps(["stop", target, msg_id]).encode()).hexdigest()
            self._insert(db, from_node, msg_id, digest, target, "user", None, "none", "push",
                         f"Stop {address}", [(node, f"stop:{address}", None)], kind="system")
        return {"id": msg_id, "to": route}

    def stop_outcome(self, from_node: str, msg_id: str) -> str | None:
        """The bridge's outcome for a stop order, or None while it has not reported one."""
        with self._db() as db:
            row = db.execute("SELECT d.outcome FROM deliveries d JOIN posts p ON p.seq = d.post WHERE p.from_node = ?"
                             " AND p.id = ? AND p.kind = 'system'", (from_node, msg_id)).fetchone()
        if row is None:
            raise HubError(404, "no such stop order")
        return row[0]

    def leave(self, node: str, topic: str, address: str) -> dict:
        with self.lock, self._db() as db:
            removed = db.execute("DELETE FROM members WHERE topic = ? AND node = ? AND address = ?",
                                 (topic, node, address)).rowcount
        return {"topic": topic, "member": f"{node}/{address}", "removed": bool(removed)}

    def topics(self) -> list[dict]:
        """Each channel: its current members, how many run now, and every session that posted in it."""
        now = time.time()
        with self._db() as db:
            members = db.execute("SELECT m.topic, m.node, m.address, m.work FROM members m WHERE m.address = 'user' OR"
                                 " EXISTS (SELECT 1 FROM sessions s WHERE s.node = m.node AND s.address = m.address)"
                                 " ORDER BY m.topic, m.joined").fetchall()
            archived = {topic for (topic,) in db.execute("SELECT topic FROM archived_topics")}
            last = dict(db.execute("SELECT topic, MAX(created) FROM posts WHERE topic IS NOT NULL GROUP BY topic"))
            authors = db.execute("SELECT topic, from_node || '/' || sender, MAX(created) FROM posts WHERE topic IS NOT NULL"
                                 " AND sender != 'user' AND IFNULL(kind, '') != 'system' GROUP BY topic, from_node, sender"
                                 " ORDER BY topic, MAX(created) DESC").fetchall()
            running = {route for route in {f"{n}/{a}" for _, n, a, _ in members if a != "user"}
                       if self._available(db, route, now)}
        listing: dict[str, dict] = {}

        def entry(topic):
            return listing.setdefault(topic, {"topic": topic, "members": [], "working": [], "running": 0,
                                              "participants": [], "last": last.get(topic),
                                              "archived": topic in archived})
        for topic, node, address, work in members:
            route = f"{node}/{address}"
            entry(topic)["members"].append(route)
            if work and address != "user":
                entry(topic)["working"].append(route)
                # Running counts the sessions that work here; watchers are not woken by its posts.
                entry(topic)["running"] += route in running
        for topic, route, _ in authors:
            entry(topic)["participants"].append(route)
        # A topic whose members all left, or that only reported history names, is still readable.
        for topic in sorted(set(last) - set(listing)):
            entry(topic)
        return list(listing.values())

    def _actor(self, db, node: str, address: object) -> str:
        """An agent acting for itself (an ack) is a session this node published; the node vouches for it."""
        if not isinstance(address, str) or not LOCAL_AGENT.fullmatch(address) or not self._published(db, node, address):
            raise HubError(403, "actor must be a session this node published (harness:id)")
        return f"{node}/{address}"

    def adopt_trusted_human(self, node: str) -> None:
        """Trust this node's "user" as the human whose posts wake every running session and get acked."""
        self.trusted_humans.add(node)

    # A topic's posts in order, numbered from 0; the hub's own instructions reach their recipient
    # through its inbox only and nobody wrote them, so they are not part of the history.
    NUMBERED = ("(SELECT p.*, ROW_NUMBER() OVER (ORDER BY p.seq) - 1 AS pos FROM posts p"
                " WHERE p.topic = ? AND p.kind IS NOT 'system')")

    def history(self, topic: str, after: int = 0, limit: int = 100, pos: int = 0) -> list[dict]:
        """A topic's posts, oldest first, after the row whose `cursor` is `after` and from position `pos`.

        The cursor is the hub's own post sequence: client ids are unique only per sending node.
        A position is the post's number in the topic, the one block summaries are built on.
        """
        with self._db() as db:
            rows = db.execute(f"SELECT seq, id, from_node, sender, target, reply, mode, text, created, reported, pos,"
                              f" attachments FROM {self.NUMBERED} WHERE seq > ? AND pos >= ? ORDER BY seq LIMIT ?",
                              (topic, after, pos, max(1, min(limit, 500)))).fetchall()
            acks = self._acks(db, [row[0] for row in rows])
        # `to` is the original target: "#topic" for a post to everyone, a route for a tagged direct message.
        # `acks` lists who read it. Replaying history wakes nobody.
        return [{"cursor": str(q), "id": i, "from": f"{fn}/{s}", "to": t, "reply": r, "mode": m, "text": x,
                 "created": c, "reported": bool(rep), "pos": n, **({"acks": acks[q]} if q in acks else {}),
                 **({"attachments": self._parsed(att)} if att else {})}
                for q, i, fn, s, t, r, m, x, c, rep, n, att in rows]

    # Block summaries ------------------------------------------------------------------------

    def _count(self, db, topic: str) -> int:
        return db.execute("SELECT COUNT(*) FROM posts WHERE topic = ? AND kind IS NOT 'system'", (topic,)).fetchone()[0]

    def _posts_at(self, db, topic: str, lo: int, hi: int) -> list[dict]:
        rows = db.execute(f"SELECT id, from_node, sender, text, created, pos FROM {self.NUMBERED}"
                          " WHERE pos >= ? AND pos < ? ORDER BY seq", (topic, lo, hi)).fetchall()
        return [{"pos": n, "id": i, "from": f"{fn}/{s}", "at": c, "text": x} for i, fn, s, x, c, n in rows]

    def _settled(self, db, topic: str) -> dict[tuple[int, int], dict]:
        return {(lo, hi): {"block": summary.label(lo, hi), "text": text, "by": author, "at": at}
                for lo, hi, text, author, at in db.execute(
                    "SELECT lo, hi, text, author, at FROM topic_summaries WHERE topic = ? ORDER BY lo, hi", (topic,))}

    def _pending(self, db, topic: str) -> dict | None:
        settled = self._settled(db, topic)
        todo = summary.pending(self._count(db, topic), settled)
        if not todo:
            return None
        lo, hi = todo[0]
        return summary.request(topic, lo, hi, {b: r["text"] for b, r in settled.items()},
                               lambda a, b: self._posts_at(db, topic, a, b), len(todo) - 1)

    def summaries(self, topic: str) -> dict:
        """A topic's post count, its settled block summaries and the block it waits for next."""
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        with self._db() as db:
            return {"topic": topic, "count": self._count(db, topic), "summaries": list(self._settled(db, topic).values()),
                    "pending": self._pending(db, topic)}

    def summarize(self, node: str, topic: str, block: str, body: dict) -> dict:
        """Settle one block's summary, written by a session this node published; the first line recorded stays."""
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        try:
            lo, hi = summary.parse_block(block)
            text = summary.check(str(body.get("text") or ""))
        except summary.SummaryError as error:
            raise HubError(400, str(error)) from None
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            author = self._actor(db, node, body.get("by"))
            settled, result = self._settled(db, topic), {"topic": topic, "block": summary.label(lo, hi)}
            if (lo, hi) in settled:
                result.update(settled=False, existing=settled[(lo, hi)]["text"])
            else:
                total = self._count(db, topic)
                if hi > total:
                    raise HubError(409, f"#{topic} holds {total} post(s); block {block} is not complete yet")
                if hi - lo > summary.LEAF and not all(half in settled for half in summary.halves(lo, hi)):
                    raise HubError(409, f"block {block} waits for its halves; summarize them first")
                db.execute("INSERT INTO topic_summaries VALUES (?, ?, ?, ?, ?, ?)", (topic, lo, hi, text, author, time.time()))
                result["settled"] = True
            result["pending"] = self._pending(db, topic)
        return result

    def open_block(self, topic: str, block: str) -> dict:
        """One block a level down: its halves' summaries, or its posts for a block of the smallest size."""
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        try:
            lo, hi = summary.parse_block(block)
        except summary.SummaryError as error:
            raise HubError(400, str(error)) from None
        with self._db() as db:
            if lo >= self._count(db, topic):
                raise HubError(404, f"#{topic} has no post #{lo} yet")
            settled = {b: r["text"] for b, r in self._settled(db, topic).items()}
            return {"topic": topic, **summary.opened(lo, hi, settled, lambda a, b: self._posts_at(db, topic, a, b))}


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
            return JSONResponse({"error": str(error)}, status_code=error.status)
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
            return await handle(request, lambda node, body, _: {"published": hub.publish(
                node, body.get("sessions"), body.get("spawn"), body.get("spawn_priority", 0))})
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
            if req.method == "DELETE":
                return hub.leave(node, topic, address)
            work = req.query_params.get("work")
            return hub.join(node, topic, address, None if work is None else work == "1")
        return await handle(request, work)

    async def history(request):
        def work(node, body, req):
            try:
                limit = int(req.query_params.get("limit", "100"))
            except ValueError:
                limit = 100
            after, pos = req.query_params.get("after", "0"), req.query_params.get("pos", "0")
            if not after.isdigit() or not pos.isdigit():
                raise HubError(400, "after is the cursor of a history row, pos a post's position in the topic")
            # SQLite integers stop at 2**63 - 1; a cursor past every post simply has nothing after it.
            return {"messages": hub.history(req.path_params["topic"], min(int(after), 2**63 - 1), limit,
                                            min(int(pos), 2**63 - 1))}
        return await handle(request, work)

    async def summaries(request):
        return await handle(request, lambda node, body, req: hub.summaries(req.path_params["topic"]))

    async def attachment(request):
        from starlette.responses import Response
        try:
            await run_in_threadpool(hub.authenticated_node, request)
            media_type, body = await run_in_threadpool(hub.attachment, request.path_params["ident"])
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        return Response(body, media_type=media_type)

    async def block(request):
        def work(node, body, req):
            topic, block = req.path_params["topic"], req.path_params["block"]
            if req.method == "PUT":
                return hub.summarize(node, topic, block, body)
            return hub.open_block(topic, block)
        return await handle(request, work)

    async def report(request):
        return await handle(request, lambda node, body, _: hub.report(node, body.get("messages")))

    async def acknowledgments(request):
        return await handle(request, lambda node, body, _: hub.acknowledge(node, body.get("actor"), body.get("id")))

    async def whoami(request):
        return await handle(request, lambda node, body, _: {"node": node})

    return Starlette(routes=[
        Route("/v1/whoami", whoami, methods=["GET"]),
        Route("/v1/sessions", sessions, methods=["GET", "PUT"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/history", report, methods=["POST"]),
        Route("/v1/messages/{msg_id}", status, methods=["GET"]),
        Route("/v1/inbox", inbox, methods=["GET"]),
        Route("/v1/acks", acks, methods=["POST"]),
        Route("/v1/acknowledgments", acknowledgments, methods=["POST"]),
        Route("/v1/topics", topics, methods=["GET"]),
        Route("/v1/topics/{topic}/members/{address:path}", membership, methods=["PUT", "DELETE"]),
        Route("/v1/topics/{topic}/messages", history, methods=["GET"]),
        Route("/v1/topics/{topic}/summaries", summaries, methods=["GET"]),
        Route("/v1/topics/{topic}/summaries/{block}", block, methods=["GET", "PUT"]),
        Route("/v1/attachments/{ident}", attachment, methods=["GET"]),
    ])
