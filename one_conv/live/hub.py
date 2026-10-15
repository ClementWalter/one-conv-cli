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

NODE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MESSAGE_ID = re.compile(r"[0-9a-f]{32}")
# What the receiving bridge reports: the adapter accepted it (not that the agent read it),
# the adapter refused it, or the bridge cannot tell (it crashed between claiming and reporting).
OUTCOMES = ("accepted", "refused", "unconfirmed")
MAX_TEXT = 200_000
MAX_WAIT = 30.0
# A node that has not published or polled for this long is listed as offline.
LEASE_SECONDS = 60.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (node TEXT PRIMARY KEY, hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (node TEXT NOT NULL, address TEXT NOT NULL, name TEXT, title TEXT, cwd TEXT,
    status TEXT, updated REAL NOT NULL, PRIMARY KEY (node, address));
CREATE TABLE IF NOT EXISTS members (topic TEXT NOT NULL, node TEXT NOT NULL, address TEXT NOT NULL,
    joined REAL NOT NULL, PRIMARY KEY (topic, node, address));
CREATE TABLE IF NOT EXISTS posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, topic TEXT, reply TEXT NOT NULL,
    text TEXT NOT NULL, created REAL NOT NULL, UNIQUE (from_node, id));
CREATE INDEX IF NOT EXISTS by_topic ON posts (topic, seq);
CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, post INTEGER NOT NULL REFERENCES posts (seq),
    to_node TEXT NOT NULL, to_address TEXT NOT NULL, taken REAL, outcome TEXT, outcome_at REAL,
    UNIQUE (post, to_node, to_address));
CREATE INDEX IF NOT EXISTS pending ON deliveries (to_node, outcome, post);
"""


class HubError(Exception):
    """A request the hub refuses, with the HTTP status to answer it with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def split_address(address: str) -> tuple[str, str]:
    """NODE/harness:id into its node and local address; refuses anything else."""
    node, sep, local = address.partition("/")
    if not sep or not NODE.fullmatch(node) or ":" not in local or "/" in local.split(":", 1)[0]:
        raise HubError(400, f"a remote address is NODE/harness:id, not {address!r}")
    return node, local


class Hub:
    """The hub's state in one SQLite file, safe for the server's threads."""

    def __init__(self, path: str):
        self.path, self.lock = path, threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)

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
            if name in names:
                raise HubError(409, f"@{name} is given to two sessions")
            names.add(name)
            rows.append((node, item["address"], name, str(item.get("title") or ""), str(item.get("cwd") or ""),
                         str(item.get("status") or ""), time.time()))
        with self.lock, self._db() as db:
            for name in names - {None}:
                if db.execute("SELECT 1 FROM sessions WHERE name = ? AND node != ?", (name, node)).fetchone():
                    raise HubError(409, f"@{name} is already used on another node")
            db.execute("DELETE FROM sessions WHERE node = ?", (node,))
            db.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            self._seen(db, node)
        return len(rows)

    def directory(self) -> list[dict]:
        """Every published session, with whether its node's bridge is still renewing its lease."""
        now = time.time()
        with self._db() as db:
            seen = dict(db.execute("SELECT node, seen FROM nodes"))
            rows = db.execute("SELECT node, address, name, title, cwd, status FROM sessions "
                              "ORDER BY node, address").fetchall()
        return [{"address": f"{node}/{address}", "node": node, "local": address, "name": name, "title": title,
                 "cwd": cwd, "status": status, "seen": seen.get(node),
                 "online": now - seen.get(node, 0.0) <= LEASE_SECONDS}
                for node, address, name, title, cwd, status in rows]

    def _published(self, db, node: str, address: str) -> bool:
        return db.execute("SELECT 1 FROM sessions WHERE node = ? AND address = ?", (node, address)).fetchone() is not None

    def _recipients(self, db, target: str, from_node: str, sender: str) -> list[tuple[str, str]]:
        """Where a message goes: one session, a named one, or every member of a topic but the sender."""
        if target.startswith("#"):
            topic = target[1:]
            members = db.execute("SELECT node, address FROM members WHERE topic = ? ORDER BY joined",
                                 (topic,)).fetchall()
            if not members:
                raise HubError(404, f"#{topic} has no members")
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
        rows = db.execute("SELECT delivery, to_node, to_address, outcome FROM deliveries WHERE post = ? "
                          "ORDER BY rowid", (seq,)).fetchall()
        return {"id": msg_id, "duplicate": duplicate,
                "recipients": [{"to": f"{n}/{a}", "delivery": d, "status": o or "stored"} for d, n, a, o in rows]}

    def send(self, node: str, message: dict) -> dict:
        """Store a post and one delivery per recipient; a retry of the same post returns its first receipt."""
        msg_id, sender, text = message.get("id"), message.get("from"), message.get("text")
        if not isinstance(msg_id, str) or not MESSAGE_ID.fullmatch(msg_id):
            raise HubError(400, "id must be 32 lowercase hex characters, chosen by the sender")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            raise HubError(400, f"text must be a non-empty string of at most {MAX_TEXT} characters")
        reply = message.get("reply", "expected")
        if reply not in ("expected", "none", "inline"):
            raise HubError(400, "reply must be expected, none or inline")
        target = message.get("to")
        if not isinstance(target, str) or not target:
            raise HubError(400, "to must be NODE/harness:id, @name or #topic")
        topic = target[1:] if target.startswith("#") else message.get("topic")
        if topic is not None and (not isinstance(topic, str) or not TOPIC.fullmatch(topic)):
            raise HubError(400, "a topic is lowercase letters, digits and dashes")
        digest = hashlib.sha256(json.dumps([target, sender, topic, reply, text]).encode()).hexdigest()
        with self.lock, self._db() as db:
            existing = db.execute("SELECT seq, digest FROM posts WHERE from_node = ? AND id = ?",
                                  (node, msg_id)).fetchone()
            if existing:
                if existing[1] != digest:
                    raise HubError(409, "this id was already used for a different message; send it under a new id")
                return self._receipt(db, existing[0], msg_id, True)
            # A sender is this node's own session or the person at its terminal, never another node's agent.
            if not isinstance(sender, str) or (not self._published(db, node, sender) and sender not in ("user", "unknown")):
                raise HubError(403, "from must be a session this node published, or user/unknown")
            recipients = self._recipients(db, target, node, sender)
            seq = db.execute("INSERT INTO posts (from_node, id, digest, target, sender, topic, reply, text, created)"
                             " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (node, msg_id, digest, target, sender, topic, reply, text, time.time())).lastrowid
            db.executemany("INSERT INTO deliveries (delivery, post, to_node, to_address) VALUES (?, ?, ?, ?)",
                           [(secrets.token_hex(16), seq, n, a) for n, a in recipients])
            return self._receipt(db, seq, msg_id, False)

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
                    "SELECT d.delivery, p.id, d.to_address, p.from_node, p.sender, p.topic, p.reply, p.text, p.created"
                    " FROM deliveries d JOIN posts p ON p.seq = d.post WHERE d.to_node = ? AND d.outcome IS NULL"
                    " ORDER BY d.post LIMIT 50", (node,)).fetchall()
                if rows:
                    db.executemany("UPDATE deliveries SET taken = ? WHERE delivery = ?",
                                   [(time.time(), r[0]) for r in rows])
            if rows or time.monotonic() >= deadline:
                return [{"delivery": d, "id": i, "to": a, "from": f"{fn}/{s}", "topic": t, "reply": r, "text": x,
                         "created": c} for d, i, a, fn, s, t, r, x, c in rows]
            time.sleep(0.25)

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
            db.execute("INSERT OR IGNORE INTO members VALUES (?, ?, ?, ?)", (topic, node, address, time.time()))
        return {"topic": topic, "member": f"{node}/{address}"}

    def leave(self, node: str, topic: str, address: str) -> dict:
        with self.lock, self._db() as db:
            removed = db.execute("DELETE FROM members WHERE topic = ? AND node = ? AND address = ?",
                                 (topic, node, address)).rowcount
        return {"topic": topic, "member": f"{node}/{address}", "removed": bool(removed)}

    def topics(self) -> list[dict]:
        with self._db() as db:
            members = db.execute("SELECT topic, node, address FROM members ORDER BY topic, joined").fetchall()
            last = dict(db.execute("SELECT topic, MAX(created) FROM posts WHERE topic IS NOT NULL GROUP BY topic"))
        listing: dict[str, dict] = {}
        for topic, node, address in members:
            listing.setdefault(topic, {"topic": topic, "members": [], "last": last.get(topic)})["members"].append(
                f"{node}/{address}")
        return list(listing.values())

    def history(self, topic: str, after: str | None, limit: int = 100) -> list[dict]:
        """A topic's posts, oldest first, after the post with id `after` if given."""
        with self._db() as db:
            since = 0
            if after:
                row = db.execute("SELECT MIN(seq) FROM posts WHERE topic = ? AND id = ?", (topic, after)).fetchone()
                since = row[0] or 0
            rows = db.execute("SELECT id, from_node, sender, reply, text, created FROM posts WHERE topic = ? AND seq > ?"
                              " ORDER BY seq LIMIT ?", (topic, since, max(1, min(limit, 500)))).fetchall()
        return [{"id": i, "from": f"{fn}/{s}", "reply": r, "text": x, "created": c} for i, fn, s, r, x, c in rows]


def application(hub: Hub):
    """The hub's HTTP API (JSON over /v1), authenticated by a per-node bearer token."""
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def handle(request: Request, work):
        try:
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else None
            node = await run_in_threadpool(hub.node_for, token)
            body = {}
            if request.method in ("POST", "PUT"):
                raw = await request.body()
                if len(raw) > MAX_TEXT * 4:
                    raise HubError(413, "request too large")
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise HubError(400, "the request body must be a JSON object")
            return JSONResponse(await run_in_threadpool(work, node, body, request))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

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
            return {"messages": hub.history(req.path_params["topic"], req.query_params.get("after"), limit)}
        return await handle(request, work)

    async def whoami(request):
        return await handle(request, lambda node, body, _: {"node": node})

    return Starlette(routes=[
        Route("/v1/whoami", whoami, methods=["GET"]),
        Route("/v1/sessions", sessions, methods=["GET", "PUT"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/messages/{msg_id}", status, methods=["GET"]),
        Route("/v1/inbox", inbox, methods=["GET"]),
        Route("/v1/acks", acks, methods=["POST"]),
        Route("/v1/topics", topics, methods=["GET"]),
        Route("/v1/topics/{topic}/members/{address:path}", membership, methods=["PUT", "DELETE"]),
        Route("/v1/topics/{topic}/messages", history, methods=["GET"]),
    ])
