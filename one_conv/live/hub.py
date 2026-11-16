"""The live relay: routes direct messages between agents on different machines.

Each machine runs one bridge (`one-conv live serve`) that authenticates with its own node
token, publishes its live sessions (with the @names they chose), and long-polls for deliveries
addressed to them.

A message is a post, kept once under (sending node, client-generated id) with a hash of its
payload, so a retry returns the first receipt and a different payload under the same id is a
conflict. A post becomes one delivery to its recipient, with its own delivery id, which the
receiving bridge acknowledges with an outcome the sender reads back with `status`.

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
MESSAGE_ID = re.compile(r"[0-9a-f]{32}")
# What the receiving bridge reports: the adapter accepted it (not that the agent read it),
# the adapter refused it, or the bridge cannot tell (it crashed between claiming and reporting).
OUTCOMES = ("accepted", "refused", "unconfirmed")
MODES = ("push", "fyi")
REPLIES = ("expected", "none", "inline")
# Images a sender attaches to a message: stored on the relay, fetched by the receiving bridge.
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
# The apps a published session can name beside its harness.
PRODUCTS = ("cursor-cli", "cursor-ide")
# CREATE IF NOT EXISTS only: a database holding other tables or extra nullable columns opens unchanged.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (node TEXT PRIMARY KEY, hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (node TEXT NOT NULL, address TEXT NOT NULL, name TEXT, title TEXT, cwd TEXT,
    status TEXT, updated REAL NOT NULL, product TEXT NOT NULL DEFAULT '', PRIMARY KEY (node, address));
CREATE TABLE IF NOT EXISTS posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, reply TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'push', text TEXT NOT NULL, created REAL NOT NULL, attachments TEXT,
    UNIQUE (from_node, id));
CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, post INTEGER NOT NULL REFERENCES posts (seq),
    to_node TEXT NOT NULL, to_address TEXT NOT NULL, taken REAL, outcome TEXT, outcome_at REAL,
    UNIQUE (post, to_node, to_address));
CREATE INDEX IF NOT EXISTS pending ON deliveries (to_node, outcome, post);
CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, node TEXT NOT NULL, media_type TEXT NOT NULL,
    size INTEGER NOT NULL, body BLOB NOT NULL, created REAL NOT NULL);
"""


class HubError(Exception):
    """A request the relay refuses, with the HTTP status to answer it with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _online(seen: float, published: float, now: float) -> bool:
    """A polling bridge cannot keep an old session publication alive."""
    return 0 <= now - seen <= LEASE_SECONDS and 0 <= now - published <= LEASE_SECONDS


def split_address(address: str) -> tuple[str, str]:
    """Split a remote session address NODE/harness:id."""
    node, sep, local = address.partition("/")
    if not sep or not NODE.fullmatch(node) or ":" not in local or "/" in local:
        raise HubError(400, f"a remote address is NODE/harness:id or @name, not {address!r}")
    return node, local


class Hub:
    """The relay's state in one SQLite file, safe for the server's threads."""

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

    # Tokens are managed on the relay's own machine; the server never creates them.
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
        """The node behind exactly one bearer authorization header."""
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
            self._seen(db, node)
        return len(rows)

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

    def _recipient(self, db, target: str) -> tuple[str, str]:
        """Where a message goes: one published session, by its route or by the @name it chose."""
        if target.startswith("@"):
            row = db.execute("SELECT node, address FROM sessions WHERE name = ?", (target[1:],)).fetchone()
            if row is None:
                raise HubError(404, f"no published session is named {target}")
            return row
        node, address = split_address(target)
        if not self._published(db, node, address):
            raise HubError(404, f"{target} is not a published session")
        return node, address

    @staticmethod
    def _receipt(db, seq: int, msg_id: str, duplicate: bool) -> dict:
        rows = db.execute("SELECT delivery, to_node, to_address, outcome FROM deliveries WHERE post = ? "
                          "ORDER BY rowid", (seq,)).fetchall()
        return {"id": msg_id, "duplicate": duplicate,
                "recipients": [{"to": f"{n}/{a}", "delivery": d, "status": o or "stored"} for d, n, a, o in rows]}

    def send(self, node: str, message: dict) -> dict:
        """Store a post and its delivery; a retry of the same post returns its first receipt."""
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
        if reply not in REPLIES:
            raise HubError(400, "reply must be expected, none or inline")
        # push wakes the receiver; fyi only files a notice for it to read when it chooses.
        mode = message.get("mode", "push")
        if mode not in MODES:
            raise HubError(400, f"mode must be one of {', '.join(MODES)}")
        target = message.get("to")
        if not isinstance(target, str) or not target:
            raise HubError(400, "to must be NODE/harness:id or @name")
        digest = hashlib.sha256(json.dumps([target, sender, reply, mode, text, *attached]).encode()).hexdigest()
        with self.lock, self._db() as db:
            # The duplicate check and the durable claim share one snapshot across server instances.
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
            to_node, to_address = self._recipient(db, target)
            seq = db.execute("INSERT INTO posts (from_node, id, digest, target, sender, reply, mode, text, created,"
                             " attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (node, msg_id, digest, target, sender, reply, mode, text, time.time(),
                              json.dumps(attachments) if attachments else None)).lastrowid
            db.execute("INSERT INTO deliveries (delivery, post, to_node, to_address) VALUES (?, ?, ?, ?)",
                       (secrets.token_hex(16), seq, to_node, to_address))
            return self._receipt(db, seq, msg_id, False)

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
                    "SELECT d.delivery, p.id, d.to_address, p.from_node, p.sender, p.reply, p.mode, p.text,"
                    " p.created, p.attachments FROM deliveries d JOIN posts p ON p.seq = d.post"
                    " WHERE d.to_node = ? AND d.outcome IS NULL ORDER BY d.post LIMIT 50", (node,)).fetchall()
                if rows:
                    db.executemany("UPDATE deliveries SET taken = ? WHERE delivery = ?",
                                   [(time.time(), r[0]) for r in rows])
            if rows or time.monotonic() >= deadline:
                return [self._handed(row) for row in rows]
            time.sleep(0.25)

    @classmethod
    def _handed(cls, row) -> dict:
        d, i, a, fn, s, r, m, x, c, attachments = row
        return {"delivery": d, "id": i, "to": a, "from": f"{fn}/{s}", "reply": r, "mode": m, "text": x,
                "created": c, **({"attachments": cls._parsed(attachments)} if attachments else {})}

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


def application(hub: Hub, archive_db: str | None = None):
    """The relay's HTTP API (JSON over /v1), authenticated by a per-node bearer token.

    With `archive_db`, the conversation archive is served at `one_conv.archive.HUB_ARCHIVE_PATH`
    under the same node tokens: every node reads every capture and uploads only under its own name.
    """
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    async def handle(request: Request, work):
        try:
            node = await run_in_threadpool(hub.authenticated_node, request)
            body = {}
            if request.method in ("POST", "PUT"):
                body = json.loads(await bounded_body(request, MAX_BODY) or b"{}")
                if not isinstance(body, dict):
                    raise HubError(400, "the request body must be a JSON object")
            return JSONResponse(await run_in_threadpool(work, node, body, request))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    async def bounded_body(request, limit: int) -> bytes:
        """The request body, refused as soon as it would exceed `limit`, whatever Content-Length says."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise HubError(413, "request too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
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

    async def upload(request):
        try:
            node = await run_in_threadpool(hub.authenticated_node, request)
            # An image is sent as itself, not inside JSON, so its limit is the image limit.
            body = await bounded_body(request, ATTACHMENT_LIMIT)
            media_type = request.headers.get("content-type", "").partition(";")[0].strip()
            return JSONResponse(await run_in_threadpool(hub.store_attachment, node, media_type, body))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)

    async def attachment(request):
        try:
            await run_in_threadpool(hub.authenticated_node, request)
            media_type, body = await run_in_threadpool(hub.attachment, request.path_params["ident"])
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        return Response(body, media_type=media_type)

    async def whoami(request):
        return await handle(request, lambda node, body, _: {"node": node})

    routes = [
        Route("/v1/whoami", whoami, methods=["GET"]),
        Route("/v1/sessions", sessions, methods=["GET", "PUT"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/messages/{msg_id}", status, methods=["GET"]),
        Route("/v1/inbox", inbox, methods=["GET"]),
        Route("/v1/acks", acks, methods=["POST"]),
        Route("/v1/attachments", upload, methods=["POST"]),
        Route("/v1/attachments/{ident}", attachment, methods=["GET"]),
    ]
    if archive_db is not None:
        routes.append(Mount(_archive_path(), app=archive_application(hub, archive_db)))
    return Starlette(routes=routes)


def _archive_path() -> str:
    from one_conv.archive import HUB_ARCHIVE_PATH
    return HUB_ARCHIVE_PATH


def archive_application(hub: Hub, path: str):
    """The conversation archive on `path`, authenticated by the relay's node tokens.

    A request without a valid node token is anonymous, which the archive refuses.
    """
    from archive.server import create_app
    from archive.store import SQLiteStore

    def node_for(request):
        try:
            return hub.authenticated_node(request)
        except HubError:
            return None

    return create_app(SQLiteStore(f"sqlite:///{path}"), node_for=node_for)
