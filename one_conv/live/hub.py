"""The live relay: routes direct messages between agents on different machines.

Each machine runs one bridge (`one-conv live serve`) that authenticates with its own node
token, publishes its live sessions (with the @names they chose), and long-polls for deliveries
addressed to them. This relay serves no /v1/stream: a bridge whose upgrade it refuses long-polls
/v1/inbox, so only the Cloudflare Worker relay (relay-worker/) pushes deliveries over a WebSocket.

A message is a post, kept once under (sending node, client-generated id) with a hash of its
payload, so a retry returns the first receipt and a different payload under the same id is a
conflict. A post becomes one delivery to its recipient, with its own delivery id, which the
receiving bridge acknowledges with an outcome the sender reads back with `status`.

A node token vouches for the sessions its bridge publishes, not for each process on that
machine. Only token hashes are stored. Delivery is at least once to the bridge; the bridge's
ingress log is what keeps a retried delivery from prompting an agent twice.

A message to `user` is for the person, not an agent: it is filed as a note with optional tags
that any of the person's nodes reads back in order through GET /v1/user, and no bridge is woken.

A connected node (or the relay's operator) can invite a new machine: POST /v1/invites returns
a one-time code bound to a node name, kept only as a digest, that expires within a day.
POST /v1/join, unauthenticated, trades the code for that node's token exactly once.

A node can share one of its published sessions with a guest (POST /v1/shares): the grant is
bound to that session's address, and only a digest of its secret is kept. Whoever holds the
link `/s/<id>#<secret>` chats with that session alone through /v1/shares/<id>/messages, the
secret in the X-Share-Secret header: a guest message becomes a delivery from `guest:<id>`
(kind "guest") to the bound session, which answers with a message to `guest:<id>`. A guest
reaches no other address, sees only its own exchange, and is refused while the session is
offline rather than queued. A guest's agent uses the same share through its MCP server,
`/s/<id>/mcp` (share_mcp.py), with the secret as its bearer token. The person gets a note tagged `share` when a share is made and
when its guest first writes.

The board (hub_board.py) keeps notes on shared topics under /v1/board: posted once, read by
whoever pulls them, never delivered to anyone.

A post with mode `request` is for a node's bridge rather than a session: `to` is a node name,
the delivery reaches that node as `to: "bridge"`, and `text` is the request (`live teleport`
asks another machine what it lacks for a session, then to start it there). The bridge answers
with its final ack, which may carry a JSON `result` the sender reads back with `status`.
A bridge that does not know requests refuses them like any address that is not a session.
"""

import asyncio
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

NODE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MESSAGE_ID = re.compile(r"[0-9a-f]{32}")
# What the receiving bridge reports: the adapter accepted it (not that the agent read it),
# the adapter refused it, or the bridge cannot tell (it crashed between claiming and reporting).
# `held` is provisional: the bridge keeps a when=idle message for a busy session and reports
# one of the others once it leaves; a held delivery is not handed out again.
OUTCOMES = ("accepted", "refused", "unconfirmed", "held")
# request: for the receiving node's bridge itself, answered with a result on the ack.
MODES = ("push", "fyi", "request")
# The address a request delivery carries: the receiving node's bridge.
BRIDGE = "bridge"
# A request's answer is small: a list of checks, or the address of a started session.
RESULT_LIMIT = 65_536
# now: delivered at once; idle: the receiving bridge holds it while the session is busy.
WHEN = ("now", "idle")
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
# The address of the person every node works for; no session can publish it.
USER = "user"
TAG = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
TAGS_PER_NOTE = 8
NAME_LIMIT = 200
NOTES_LIMIT = 200
# An invite is short-lived: long enough to walk to the other machine, never a standing credential.
INVITE_TTL = 15 * 60
INVITE_TTL_MIN = 60
INVITE_TTL_MAX = 24 * 3600
# Used and expired invites are kept this long for the operator to see, then pruned.
INVITE_KEEP = 7 * 24 * 3600
MAX_CODE = 256
# Shares: a guest link to one session, valid a day by default and a month at most.
SHARE_ID = re.compile(r"[0-9a-f]{16}")
GUEST = "guest:"
SHARE_TTL = 24 * 3600
SHARE_TTL_MIN = 60
SHARE_TTL_MAX = 30 * 24 * 3600
# Expired and revoked shares, with their exchange, are kept this long for the owner to list.
SHARE_KEEP = 7 * 24 * 3600
LABEL_LIMIT = 64
# A guest sends text only, at most GUEST_TEXT bytes, GUEST_RATE messages per GUEST_WINDOW seconds.
GUEST_TEXT = 8192
GUEST_BODY = 64 * 1024
GUEST_RATE = 20
GUEST_WINDOW = 300
GUEST_PAGE = 200
# A guest's GET waits at most this long for an answer; the share's MCP `ask` waits by checking every GUEST_POLL.
GUEST_WAIT = 30.0
GUEST_POLL = 0.25
MAX_SECRET = 256
SHARE_HEADER = "x-share-secret"
INACTIVE = "this link is no longer active"
SHARE_TAG = "share"
# The guest page: one static file this relay and the Worker both serve.
SHARE_PAGE = Path(__file__).resolve().parents[2] / "relay-worker" / "src" / "share-page.html"
# The page runs only its own inline code and talks only to this origin.
PAGE_HEADERS = {
    "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
}
# CREATE IF NOT EXISTS only: a database holding other tables or extra nullable columns opens unchanged.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (node TEXT PRIMARY KEY, hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (node TEXT NOT NULL, address TEXT NOT NULL, name TEXT, title TEXT, cwd TEXT,
    status TEXT, updated REAL NOT NULL, product TEXT NOT NULL DEFAULT '', PRIMARY KEY (node, address));
CREATE TABLE IF NOT EXISTS posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, reply TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'push', text TEXT NOT NULL, created REAL NOT NULL, attachments TEXT,
    timing TEXT NOT NULL DEFAULT 'now', UNIQUE (from_node, id));
CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, post INTEGER NOT NULL REFERENCES posts (seq),
    to_node TEXT NOT NULL, to_address TEXT NOT NULL, taken REAL, outcome TEXT, outcome_at REAL, result TEXT,
    UNIQUE (post, to_node, to_address));
CREATE INDEX IF NOT EXISTS pending ON deliveries (to_node, outcome, post);
CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, node TEXT NOT NULL, media_type TEXT NOT NULL,
    size INTEGER NOT NULL, body BLOB NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notes (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,
    digest TEXT NOT NULL, sender TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL, tags TEXT NOT NULL,
    created REAL NOT NULL, UNIQUE (from_node, id));
CREATE TABLE IF NOT EXISTS invites (hash TEXT PRIMARY KEY, node TEXT NOT NULL, inviter TEXT NOT NULL,
    takes_over INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, used REAL);
CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, hash TEXT NOT NULL, node TEXT NOT NULL, address TEXT NOT NULL,
    label TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, revoked REAL, first_used REAL);
CREATE TABLE IF NOT EXISTS share_messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, share TEXT NOT NULL,
    side TEXT NOT NULL, from_node TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, text TEXT NOT NULL,
    created REAL NOT NULL, post INTEGER, UNIQUE (from_node, id));
CREATE INDEX IF NOT EXISTS share_exchange ON share_messages (share, seq);
"""
# A guest's post has no sending node; its sender is guest:<share id>, whose share gives the label.
PENDING = ("SELECT d.delivery, p.id, d.to_address, p.from_node, p.sender, p.reply, p.mode, p.text, p.created,"
           " p.attachments, p.timing, s.label FROM deliveries d JOIN posts p ON p.seq = d.post"
           " LEFT JOIN shares s ON p.from_node = '' AND p.sender = 'guest:' || s.id")


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
            # A relay database from before transcript grants gains the flag, every old share being messaging only.
            if "transcript" not in {row[1] for row in db.execute("PRAGMA table_info(shares)")}:
                db.execute("ALTER TABLE shares ADD COLUMN transcript INTEGER NOT NULL DEFAULT 0")
            # A relay database from before `when` existed gains the column, every old post being `now`.
            if "timing" not in {row[1] for row in db.execute("PRAGMA table_info(posts)")}:
                db.execute("ALTER TABLE posts ADD COLUMN timing TEXT NOT NULL DEFAULT 'now'")
            # A relay database from before requests existed gains the column for their answers.
            if "result" not in {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}:
                db.execute("ALTER TABLE deliveries ADD COLUMN result TEXT")
        from .hub_board import SCHEMA as BOARD_SCHEMA, Board
        with self._db() as db:
            db.executescript(BOARD_SCHEMA)
        self.board = Board(self)

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

    def create_invite(self, by: str, node, ttl=INVITE_TTL, replace=False) -> dict:
        """A one-time code that `redeem_invite` trades for NODE's token; `by` is the inviting node ("" for the operator).

        A node that already has a token is refused unless `replace`, so an invite never
        silently takes over a running machine's identity.
        """
        if not isinstance(node, str) or not NODE.fullmatch(node):
            raise HubError(400, f"node names are lowercase letters, digits and dashes: {node!r}")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not INVITE_TTL_MIN <= ttl <= INVITE_TTL_MAX:
            raise HubError(400, f"ttl is a whole number of seconds from {INVITE_TTL_MIN} to {INVITE_TTL_MAX}")
        if not isinstance(replace, bool):
            raise HubError(400, "replace is true or false")
        code, now = secrets.token_urlsafe(24), time.time()
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not replace and db.execute("SELECT 1 FROM tokens WHERE node = ?", (node,)).fetchone():
                raise HubError(409, f"node {node} already has a token; invite with replace to take it over")
            db.execute("DELETE FROM invites WHERE expires < ?", (now - INVITE_KEEP,))
            db.execute("INSERT INTO invites VALUES (?, ?, ?, ?, ?, ?, NULL)",
                       (_hash(code), node, by, int(replace), now, now + ttl))
        return {"node": node, "code": code, "expires": now + ttl}

    def redeem_invite(self, code) -> dict:
        """NODE's new token for an unused, unexpired invite code; the code is spent in the same transaction."""
        if not isinstance(code, str) or not code or len(code) > MAX_CODE:
            raise HubError(400, "code is the invite code `one-conv live invite` printed")
        digest = _hash(code)
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT hash, node, takes_over, expires, used FROM invites").fetchall()
            found = next((row for row in rows if hmac.compare_digest(row[0], digest)), None)
            if found is None:
                raise HubError(404, "unknown invite code")
            stored, node, replace, expires, used = found
            if used is not None:
                raise HubError(410, "this invite was already used; ask for a new one with `one-conv live invite`")
            if expires < time.time():
                raise HubError(410, "this invite expired; ask for a new one with `one-conv live invite`")
            if not replace and db.execute("SELECT 1 FROM tokens WHERE node = ?", (node,)).fetchone():
                raise HubError(409, f"node {node} got a token after this invite was made; ask for one with --replace")
            token = secrets.token_urlsafe(32)
            db.execute("UPDATE invites SET used = ? WHERE hash = ?", (time.time(), stored))
            db.execute("INSERT OR REPLACE INTO tokens VALUES (?, ?, ?)", (node, _hash(token), time.time()))
        return {"node": node, "token": token}

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
    def _bridge_recipient(db, target: str) -> tuple[str, str]:
        """Where a request goes: the bridge of a node that holds a token on this relay."""
        if not NODE.fullmatch(target):
            raise HubError(400, f"a request goes to a node name, not {target!r}")
        if db.execute("SELECT 1 FROM tokens WHERE node = ?", (target,)).fetchone() is None:
            raise HubError(404, f"no node is called {target}")
        return target, BRIDGE

    @staticmethod
    def _receipt(db, seq: int, msg_id: str, duplicate: bool) -> dict:
        rows = db.execute("SELECT delivery, to_node, to_address, outcome FROM deliveries WHERE post = ? "
                          "ORDER BY rowid", (seq,)).fetchall()
        timing = db.execute("SELECT timing FROM posts WHERE seq = ?", (seq,)).fetchone()[0]
        # Echoing `when` tells the sender this relay carries it.
        return {"id": msg_id, "duplicate": duplicate, **({"when": timing} if timing == "idle" else {}),
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
        timing = message.get("when", "now")
        if timing not in WHEN:
            raise HubError(400, f"when must be one of {', '.join(WHEN)}")
        target = message.get("to")
        if not isinstance(target, str) or not target:
            raise HubError(400, "to must be NODE/harness:id, @name or user")
        if target == USER:
            return self._note(node, msg_id, sender, text, message, bool(attached))
        if target.startswith(GUEST):
            return self._guest_reply(node, msg_id, sender, text, target, bool(attached))
        if mode == "request" and attached:
            raise HubError(400, "a request carries text only")
        # `when` joins the digest only when idle, so a `now` post hashes as it always has.
        fields = [target, sender, reply, mode, text, *attached] + (["when=idle"] if timing == "idle" else [])
        digest = hashlib.sha256(json.dumps(fields).encode()).hexdigest()
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
            to_node, to_address = (self._bridge_recipient(db, target) if mode == "request"
                                   else self._recipient(db, target))
            seq = db.execute("INSERT INTO posts (from_node, id, digest, target, sender, reply, mode, text, created,"
                             " attachments, timing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (node, msg_id, digest, target, sender, reply, mode, text, time.time(),
                              json.dumps(attachments) if attachments else None, timing)).lastrowid
            db.execute("INSERT INTO deliveries (delivery, post, to_node, to_address) VALUES (?, ?, ?, ?)",
                       (secrets.token_hex(16), seq, to_node, to_address))
            return self._receipt(db, seq, msg_id, False)

    def _note(self, node: str, msg_id: str, sender, text: str, message: dict, attached: bool) -> dict:
        """File a message to the person; a retry of the same note returns its first receipt."""
        if attached:
            raise HubError(400, "a message to user carries text only")
        tags = message.get("tags") or []
        if (not isinstance(tags, list) or len(tags) > TAGS_PER_NOTE
                or not all(isinstance(t, str) and TAG.fullmatch(t) for t in tags)):
            raise HubError(400, f"tags is a list of at most {TAGS_PER_NOTE} lowercase words (letters, digits, dashes)")
        name = message.get("name") or ""
        if not isinstance(name, str) or len(name) > NAME_LIMIT:
            raise HubError(400, f"name is a string of at most {NAME_LIMIT} characters")
        unique = list(dict.fromkeys(tags))
        digest = hashlib.sha256(json.dumps([USER, sender, name, text, *unique]).encode()).hexdigest()
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT digest FROM notes WHERE from_node = ? AND id = ?", (node, msg_id)).fetchone()
            if existing and existing[0] != digest:
                raise HubError(409, "this id was already used for a different message; send it under a new id")
            if not existing:
                if not isinstance(sender, str) or (not self._published(db, node, sender) and sender not in ("user", "unknown")):
                    raise HubError(403, "from must be a session this node published, or user/unknown")
                db.execute("INSERT INTO notes (from_node, id, digest, sender, name, text, tags, created)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (node, msg_id, digest, sender, name, text, json.dumps(unique), time.time()))
            return {"id": msg_id, "duplicate": bool(existing), "recipients": [{"to": USER, "status": "stored"}]}

    def notes(self, after: int = 0, tags: list[str] = (), limit: int = NOTES_LIMIT) -> dict:
        """Notes after `after`, oldest first, at most `limit`; `tags` keeps notes carrying any of them.

        `last` is the newest note's seq overall, so a reader's cursor can move past notes its filter skipped.
        """
        if not all(TAG.fullmatch(t) for t in tags):
            raise HubError(400, "a tag is lowercase letters, digits and dashes")
        limit = max(1, min(limit, NOTES_LIMIT))
        tag_filter = (f" AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value IN ({', '.join('?' * len(tags))}))"
                      if tags else "")
        with self._db() as db:
            rows = db.execute("SELECT seq, id, from_node, sender, name, text, tags, created FROM notes"
                              f" WHERE seq > ?{tag_filter} ORDER BY seq LIMIT ?", (max(0, after), *tags, limit)).fetchall()
            last = db.execute("SELECT MAX(seq) FROM notes").fetchone()[0] or 0
        return {"messages": [{"seq": seq, "id": ident, "from": f"{from_node}/{sender}", "name": name, "text": text,
                              "tags": json.loads(tag_list), "created": created}
                             for seq, ident, from_node, sender, name, text, tag_list, created in rows],
                "last": last}

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
                rows = db.execute(f"{PENDING} WHERE d.to_node = ? AND d.outcome IS NULL ORDER BY d.post LIMIT 50",
                                  (node,)).fetchall()
                if rows:
                    db.executemany("UPDATE deliveries SET taken = ? WHERE delivery = ?",
                                   [(time.time(), r[0]) for r in rows])
            if rows or time.monotonic() >= deadline:
                return [self._handed(row) for row in rows]
            time.sleep(0.25)

    @classmethod
    def _handed(cls, row) -> dict:
        d, i, a, fn, s, r, m, x, c, attachments, timing, label = row
        # A guest is named by its share alone: it has no node and no session of its own.
        guest = {"kind": "guest", "label": label or ""} if fn == "" else {}
        return {"delivery": d, "id": i, "to": a, "from": s if fn == "" else f"{fn}/{s}", "reply": r, "mode": m,
                "text": x, "created": c, **({"attachments": cls._parsed(attachments)} if attachments else {}),
                **({"when": timing} if timing == "idle" else {}), **guest}

    def ack(self, node: str, delivery: str, outcome: str, result=None) -> dict:
        """Record the outcome of one of this node's deliveries; the first final outcome reported stands.

        `held` is recorded only on a delivery without an outcome, and a final outcome replaces it.
        `result` is a JSON object answering a request, kept with the outcome it came with.
        """
        if outcome not in OUTCOMES:
            raise HubError(400, f"outcome must be one of {', '.join(OUTCOMES)}")
        stored = None
        if result is not None:
            if not isinstance(result, dict):
                raise HubError(400, "result is a JSON object")
            stored = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
            if len(stored) > RESULT_LIMIT:
                raise HubError(413, f"a result is at most {RESULT_LIMIT} characters of JSON")
        with self.lock, self._db() as db:
            row = db.execute("SELECT d.outcome, p.mode FROM deliveries d JOIN posts p ON p.seq = d.post"
                             " WHERE d.delivery = ? AND d.to_node = ?", (delivery, node)).fetchone()
            if row is None:
                raise HubError(404, "no such delivery for this node")
            if stored is not None and row[1] != "request":
                raise HubError(400, "only a request is answered with a result")
            db.execute("UPDATE deliveries SET outcome = ?, outcome_at = ?, result = COALESCE(?, result)"
                       " WHERE delivery = ? AND (outcome IS NULL OR (outcome = 'held' AND ? != 'held'))",
                       (outcome, time.time(), stored, delivery, outcome))
            current = db.execute("SELECT outcome FROM deliveries WHERE delivery = ?", (delivery,)).fetchone()[0]
        return {"delivery": delivery, "outcome": current}

    def status(self, node: str, msg_id: str) -> dict:
        """The outcome per recipient of a message this node sent; other nodes cannot read it."""
        with self._db() as db:
            post = db.execute("SELECT seq FROM posts WHERE from_node = ? AND id = ?", (node, msg_id)).fetchone()
            if post is None:
                raise HubError(404, "no such message from this node")
            rows = db.execute("SELECT delivery, to_node, to_address, outcome, outcome_at, result FROM deliveries"
                              " WHERE post = ? ORDER BY rowid", (post[0],)).fetchall()
        return {"id": msg_id, "recipients": [{"to": f"{n}/{a}", "delivery": d, "outcome": o or "stored",
                                              "outcome_at": at, **({"result": json.loads(r)} if r else {})}
                                             for d, n, a, o, at, r in rows]}

    # --- shares: a guest link to one session -------------------------------------------------------

    @staticmethod
    def _file_note(db, node: str, sender: str, name: str, text: str) -> None:
        """A note the relay itself files for the person, tagged `share`."""
        ident = secrets.token_hex(16)
        digest = hashlib.sha256(json.dumps([USER, sender, name, text, SHARE_TAG]).encode()).hexdigest()
        db.execute("INSERT INTO notes (from_node, id, digest, sender, name, text, tags, created)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (node, ident, digest, sender, name, text, json.dumps([SHARE_TAG]), time.time()))

    @staticmethod
    def _session_state(db, node: str, address: str) -> dict | None:
        """The published session's title, name, status and whether its bridge holds it online; None if unpublished."""
        row = db.execute("SELECT name, title, status, updated FROM sessions WHERE node = ? AND address = ?",
                         (node, address)).fetchone()
        if row is None:
            return None
        seen = db.execute("SELECT seen FROM nodes WHERE node = ?", (node,)).fetchone()
        return {"name": row[0], "title": row[1] or "", "status": row[2] or "",
                "online": _online(seen[0] if seen else 0.0, row[3], time.time())}

    def create_share(self, node: str, address, label="guest", ttl=SHARE_TTL, transcript=False) -> dict:
        """A guest link bound to one session this node published and holds online; the secret is returned once.

        `transcript` also lets the guest read and search that session's archived transcript, and nothing else.
        """
        if not isinstance(address, str) or not address:
            raise HubError(400, "address is the session to share, harness:id or NODE/harness:id")
        local = address
        if "/" in address:
            owner, local = split_address(address)
            if owner != node:
                raise HubError(403, f"{address} is another node's session; share it from that machine")
        if ":" not in local:
            raise HubError(400, f"a session address is harness:id, not {address!r}")
        if not isinstance(label, str) or not label.strip() or len(label) > LABEL_LIMIT \
                or any(ord(char) < 32 or ord(char) == 127 for char in label):
            raise HubError(400, f"label is a name of at most {LABEL_LIMIT} printable characters")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not SHARE_TTL_MIN <= ttl <= SHARE_TTL_MAX:
            raise HubError(400, f"ttl is a whole number of seconds from {SHARE_TTL_MIN} to {SHARE_TTL_MAX}")
        if not isinstance(transcript, bool):
            raise HubError(400, "transcript is true or false")
        label = label.strip()
        ident, secret, now = secrets.token_hex(8), secrets.token_urlsafe(32), time.time()
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._session_state(db, node, local)
            if state is None or not state["online"]:
                raise HubError(409, f"{node}/{local} is not reachable through the relay: its machine's bridge must"
                                    " be running (`one-conv live serve`)")
            stale = [row[0] for row in db.execute("SELECT id FROM shares WHERE expires < ?", (now - SHARE_KEEP,))]
            db.executemany("DELETE FROM share_messages WHERE share = ?", [(s,) for s in stale])
            db.executemany("DELETE FROM shares WHERE id = ?", [(s,) for s in stale])
            db.execute("INSERT INTO shares (id, hash, node, address, label, created, expires, transcript)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (ident, _hash(secret), node, local, label, now, now + ttl,
                                                          int(transcript)))
            until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now + ttl))
            scope = " with transcript" if transcript else ""
            self._file_note(db, node, local, label,
                            f"Shared {node}/{local}{scope} with {label} until {until} (share {ident}). "
                            f"Revoke it with `one-conv live share --revoke {ident} --yes`.")
        return {"id": ident, "secret": secret, "address": f"{node}/{local}", "label": label,
                "expires": now + ttl, "path": f"/s/{ident}", "transcript": transcript}

    def list_shares(self, node: str) -> dict:
        """This node's shares, newest first, with their state and how many messages each guest sent."""
        now = time.time()
        with self._db() as db:
            rows = db.execute("SELECT s.id, s.address, s.label, s.created, s.expires, s.revoked, s.first_used,"
                              " s.transcript, (SELECT COUNT(*) FROM share_messages m WHERE m.share = s.id AND m.side = 'guest')"
                              " FROM shares s WHERE s.node = ? ORDER BY s.created DESC", (node,)).fetchall()
        return {"shares": [{"id": i, "address": f"{node}/{a}", "label": label, "created": c, "expires": e,
                            "revoked": r, "first_used": used, "guest_messages": count, "transcript": bool(t),
                            "state": "revoked" if r is not None else ("expired" if e < now else "active")}
                           for i, a, label, c, e, r, used, t, count in rows]}

    def revoke_share(self, node: str, ident: str) -> dict:
        """End one of this node's shares at once; its page then says the link is no longer active."""
        with self.lock, self._db() as db:
            changed = db.execute("UPDATE shares SET revoked = COALESCE(revoked, ?) WHERE id = ? AND node = ?",
                                 (time.time(), ident, node)).rowcount
        if not changed:
            raise HubError(404, f"no share {ident} made by this node")
        return {"id": ident, "revoked": True}

    @staticmethod
    def _active_share(db, ident: str, secret) -> tuple:
        """(node, address, label, expires, first_used, transcript) of a share whose secret matches; 404 or 410 otherwise.

        An unknown id and a wrong secret answer alike, so a guess learns nothing.
        """
        if not SHARE_ID.fullmatch(ident) or not isinstance(secret, str) or not secret or len(secret) > MAX_SECRET:
            raise HubError(404, INACTIVE)
        row = db.execute("SELECT hash, node, address, label, expires, revoked, first_used, transcript FROM shares"
                         " WHERE id = ?",
                         (ident,)).fetchone()
        # Compared even without a row, so an unknown id takes as long as a wrong secret.
        if not hmac.compare_digest(row[0] if row else "0" * 64, _hash(secret)) or row is None:
            raise HubError(404, INACTIVE)
        if row[5] is not None or row[4] < time.time():
            raise HubError(410, INACTIVE)
        return row[1], row[2], row[3], row[4], row[6], bool(row[7])

    def share_grant(self, ident: str, secret) -> tuple[str, str, bool]:
        """(node, local address, transcript granted) of an active share whose secret matches."""
        with self._db() as db:
            node, address, _, _, _, transcript = self._active_share(db, ident, secret)
        return node, address, transcript

    def guest_view(self, ident: str, secret, after: int = 0) -> dict:
        """The share's label, its session's @name and state, and the exchange after `after`, oldest first.

        The session's title is part of its content, so it shows only on a share that grants the transcript.
        """
        with self._db() as db:
            node, address, label, expires, _, transcript = self._active_share(db, ident, secret)
            state = self._session_state(db, node, address) or {"name": None, "title": "", "status": "", "online": False}
            rows = db.execute("SELECT m.seq, m.side, m.text, m.created, d.outcome FROM share_messages m"
                              " LEFT JOIN deliveries d ON d.post = m.post WHERE m.share = ? AND m.seq > ?"
                              " ORDER BY m.seq LIMIT ?", (ident, max(0, after), GUEST_PAGE)).fetchall()
        return {"share": {"label": label, "title": state["title"] if transcript else "", "name": state["name"],
                          "online": state["online"], "busy": state["status"] in ("busy", "waiting"), "expires": expires,
                          "transcript": transcript},
                "messages": [{"seq": seq, "from": side, "text": text, "created": created,
                              **({"status": outcome or "sent"} if side == "guest" else {})}
                             for seq, side, text, created, outcome in rows]}

    def guest_send(self, ident: str, secret, text) -> dict:
        """Route a guest's text to the share's session as a delivery from guest:<id>; refused while it is offline."""
        if not isinstance(text, str) or not text.strip():
            raise HubError(400, "text is the message to send")
        if len(text.encode("utf-8", "surrogatepass")) > GUEST_TEXT:
            raise HubError(413, f"a message is at most {GUEST_TEXT // 1024} KB")
        now = time.time()
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            node, address, label, _, first_used, _ = self._active_share(db, ident, secret)
            state = self._session_state(db, node, address)
            if state is None or not state["online"]:
                raise HubError(409, "the agent is offline; this message was not sent")
            recent = db.execute("SELECT COUNT(*) FROM share_messages WHERE share = ? AND side = 'guest' AND created > ?",
                                (ident, now - GUEST_WINDOW)).fetchone()[0]
            if recent >= GUEST_RATE:
                raise HubError(429, f"at most {GUEST_RATE} messages per {GUEST_WINDOW // 60} minutes; wait a little")
            msg_id, sender = secrets.token_hex(16), f"{GUEST}{ident}"
            digest = hashlib.sha256(json.dumps([f"{node}/{address}", sender, text]).encode()).hexdigest()
            seq = db.execute("INSERT INTO posts (from_node, id, digest, target, sender, reply, mode, text, created,"
                             " attachments, timing) VALUES ('', ?, ?, ?, ?, 'expected', 'push', ?, ?, NULL, 'now')",
                             (msg_id, digest, f"{node}/{address}", sender, text, now)).lastrowid
            db.execute("INSERT INTO deliveries (delivery, post, to_node, to_address) VALUES (?, ?, ?, ?)",
                       (secrets.token_hex(16), seq, node, address))
            row = db.execute("INSERT INTO share_messages (share, side, from_node, id, digest, text, created, post)"
                             " VALUES (?, 'guest', '', ?, ?, ?, ?, ?)", (ident, msg_id, digest, text, now, seq)).lastrowid
            if first_used is None:
                db.execute("UPDATE shares SET first_used = ? WHERE id = ?", (now, ident))
                self._file_note(db, node, sender, label,
                                f"{label} sent a first message through share {ident} to {node}/{address}.")
        return {"seq": row, "status": "sent"}

    def _guest_reply(self, node: str, msg_id: str, sender, text: str, target: str, attached: bool) -> dict:
        """File the shared session's answer for its guest's page; only that session (or its owner) may answer."""
        if attached:
            raise HubError(400, "a message to a guest carries text only")
        ident = target[len(GUEST):]
        if not SHARE_ID.fullmatch(ident):
            raise HubError(400, f"a guest address is guest:<share id>, not {target!r}")
        digest = hashlib.sha256(json.dumps([target, sender, text]).encode()).hexdigest()
        receipt = {"id": msg_id, "recipients": [{"to": target, "status": "stored"}]}
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT digest FROM share_messages WHERE from_node = ? AND id = ?",
                                  (node, msg_id)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise HubError(409, "this id was already used for a different message; send it under a new id")
                return {**receipt, "duplicate": True}
            row = db.execute("SELECT node, address, expires, revoked FROM shares WHERE id = ?", (ident,)).fetchone()
            if row is None:
                raise HubError(404, f"no share {ident}")
            if row[0] != node or sender not in (row[1], USER):
                raise HubError(403, f"only the shared session {row[0]}/{row[1]} answers {target}")
            if row[3] is not None or row[2] < time.time():
                raise HubError(410, f"share {ident} is no longer active; the guest cannot read new messages")
            db.execute("INSERT INTO share_messages (share, side, from_node, id, digest, text, created, post)"
                       " VALUES (?, 'agent', ?, ?, ?, ?, ?, NULL)", (ident, node, msg_id, digest, text, time.time()))
        return {**receipt, "duplicate": False}


def application(hub: Hub, archive_db: str | None = None):
    """The relay's HTTP API (JSON over /v1), authenticated by a per-node bearer token.

    With `archive_db`, the conversation archive is served at `one_conv.archive.HUB_ARCHIVE_PATH`
    under the same node tokens: every node reads every capture and uploads only under its own name.
    It also serves the transcript of a shared session to a share that grants it; without
    `archive_db` no share can grant a transcript.
    """
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route
    from urllib.parse import quote

    from .hub_board import FILE_LIMIT, file_name, parse_reads

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
                                                                    str(body.get("outcome")), body.get("result")))

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

    async def invites(request):
        return await handle(request, lambda node, body, _: hub.create_invite(
            node, body.get("node"), body.get("ttl", INVITE_TTL), body.get("replace", False)))

    async def join(request):
        # No token: the invite code in the body is the credential, and it is spent on success.
        try:
            body = json.loads(await bounded_body(request, 4096) or b"{}")
            if not isinstance(body, dict):
                raise HubError(400, "the request body must be a JSON object")
            return JSONResponse(await run_in_threadpool(hub.redeem_invite, body.get("code")))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    async def whoami(request):
        return await handle(request, lambda node, body, _: {"node": node})

    def int_param(request, name: str, default: int) -> int:
        try:
            return int(float(request.query_params.get(name, str(default))))
        except ValueError:
            return default

    async def user_notes(request):
        return await handle(request, lambda node, body, req: hub.notes(int_param(req, "after", 0),
                                                                      req.query_params.getlist("tag"),
                                                                      int_param(req, "limit", NOTES_LIMIT)))

    async def board_topics(request):
        return await handle(request, lambda node, body, req: hub.board.topics(parse_reads(req.query_params.getlist("read"))))

    async def board_topic(request):
        topic = request.path_params["topic"]
        if request.method == "POST":
            return await handle(request, lambda node, body, _: hub.board.post(node, topic, body))
        return await handle(request, lambda node, body, req: hub.board.read(
            topic, int_param(req, "after", 0), req.query_params.getlist("tag"), int_param(req, "limit", 50)))

    async def board_search(request):
        return await handle(request, lambda node, body, req: hub.board.search(
            req.query_params.get("q", ""), req.query_params.get("topic") or None,
            req.query_params.get("words", "") in ("1", "true"), int_param(req, "limit", 20)))

    async def board_delete(request):
        params = request.path_params
        return await handle(request, lambda node, body, _: hub.board.delete(node, params["topic"], params["ident"]))

    async def board_upload(request):
        try:
            node = await run_in_threadpool(hub.authenticated_node, request)
            # A file is sent as itself, not inside JSON, so its limit is the file limit.
            body = await bounded_body(request, FILE_LIMIT)
            name = file_name(request.headers.get("x-file-name", ""))
            return JSONResponse(await run_in_threadpool(hub.board.upload, node, request.path_params["topic"], name,
                                                        request.headers.get("content-type", ""), body))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)

    async def board_file(request):
        try:
            await run_in_threadpool(hub.authenticated_node, request)
            meta, body = await run_in_threadpool(hub.board.file, request.path_params["topic"], request.path_params["ident"])
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        # Served as a download, never rendered: a file is whatever a node uploaded.
        return Response(body, media_type=meta["content_type"], headers={
            "content-disposition": f"attachment; filename*=UTF-8''{quote(meta['name'], safe='')}",
            "x-content-type-options": "nosniff", "x-content-sha256": meta["sha256"]})

    store = None
    if archive_db is not None:
        from archive.store import SQLiteStore
        store = SQLiteStore(f"sqlite:///{archive_db}")

    def create_share(node, body):
        if body.get("transcript") is True and store is None:
            raise HubError(409, "this relay keeps no archive, so a share cannot grant a transcript")
        return hub.create_share(node, body.get("address"), body.get("label", "guest"), body.get("ttl", SHARE_TTL),
                                body.get("transcript", False))

    async def shares(request):
        if request.method == "POST":
            return await handle(request, lambda node, body, _: create_share(node, body))
        return await handle(request, lambda node, body, _: hub.list_shares(node))

    async def share(request):
        return await handle(request, lambda node, body, req: hub.revoke_share(node, req.path_params["share"]))

    def share_secret(request):
        # Exactly one header: the secret never travels in a URL, where logs and history would keep it.
        values = request.headers.getlist(SHARE_HEADER)
        return values[0] if len(values) == 1 else None

    async def wait_reply(ident, secret, after: int, seconds: float) -> dict:
        """The guest view once the session answered after `after`, or when `seconds` ran out.

        It checks every GUEST_POLL seconds without holding a server thread, and a revoked or
        expired share ends the wait with its error.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            view = await run_in_threadpool(hub.guest_view, ident, secret, after)
            if any(m["from"] == "agent" for m in view["messages"]) or time.monotonic() >= deadline:
                return view
            await asyncio.sleep(GUEST_POLL)

    async def guest(request):
        # No node token: the share's secret is the credential, and it reaches only the bound session.
        try:
            ident, secret = request.path_params["share"], share_secret(request)
            if request.method == "GET":
                # `wait` holds the request until the session answers after `after`, as an inbox long-poll does.
                seconds = min(max(float_param(request, "wait"), 0.0), GUEST_WAIT)
                return JSONResponse(await wait_reply(ident, secret, int_param(request, "after", 0), seconds),
                                    headers={"cache-control": "no-store"})
            body = json.loads(await bounded_body(request, GUEST_BODY) or b"{}")
            if not isinstance(body, dict):
                raise HubError(400, "the request body must be a JSON object")
            return JSONResponse(await run_in_threadpool(hub.guest_send, ident, secret, body.get("text")))
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    async def page(request):
        return Response(SHARE_PAGE.read_bytes(), media_type="text/html; charset=utf-8", headers=PAGE_HEADERS)

    def float_param(request, name: str) -> float:
        try:
            value = float(request.query_params.get(name, "0"))
        except ValueError:
            return 0.0
        return value if math.isfinite(value) else 0.0

    def granted_turns(ident, secret, raw: bool) -> tuple[list[dict], float]:
        """(rendered turns, archived mtime) of the one session the share names, if it grants the transcript."""
        from . import share_transcript
        node, address, granted = hub.share_grant(ident, secret)
        if not granted:
            raise HubError(403, "this share does not grant the session's transcript")
        if store is None:
            raise HubError(503, "this relay keeps no archive")
        try:
            turns, mtime = share_transcript.load(store, node, address)
        except share_transcript.TranscriptMissing as error:
            raise HubError(404, str(error)) from None
        return share_transcript.render_turns(turns, raw), mtime

    def read_transcript(ident, secret, offset=None, limit=40, around=None, raw=False) -> dict:
        """A page of the shared session's turns, as the remote MCP's read_conversation pages them."""
        from . import share_transcript
        turns, mtime = granted_turns(ident, secret, raw)
        start = share_transcript.page_start(turns, limit, offset, around)
        return {**share_transcript.page(turns, start, limit), "archived_through": mtime}

    def search_transcript(ident, secret, query: str, limit=20) -> dict:
        """Turns of the shared session holding every word of `query`, newest first; hidden blocks are never searched."""
        from . import share_transcript
        turns, mtime = granted_turns(ident, secret, False)
        return {**share_transcript.search(turns, query, limit), "archived_through": mtime}

    def archived_through(ident, secret) -> float | None:
        from . import share_transcript
        node, address, granted = hub.share_grant(ident, secret)
        head = share_transcript.archived(store, node, address) if granted and store is not None else None
        return head["mtime"] if head else None

    def transcript_params(request) -> dict:
        """read_transcript's arguments from a guest GET, refused as the MCP tool refuses them."""
        from . import share_mcp as protocol
        query = request.query_params
        args = {"raw": query.get("raw") in ("1", "true")}
        try:
            if "offset" in query:
                args["offset"] = int(query["offset"])
            if "limit" in query:
                args["limit"] = int(query["limit"])
        except ValueError:
            raise HubError(400, "offset and limit are whole numbers") from None
        if "around" in query:
            args["around"] = query["around"]
        try:
            return protocol.read_arguments(args)
        except protocol.ToolRefused as error:
            raise HubError(400, str(error)) from None

    async def transcript(request):
        # The share's secret is the credential, as for the guest's messages; the session is the grant's alone.
        from . import share_mcp as protocol
        try:
            ident, secret = request.path_params["share"], share_secret(request)
            if request.url.path.endswith("/search"):
                try:
                    query, limit = protocol.search_arguments({"query": request.query_params.get("query", ""),
                                                              "limit": int_param(request, "limit", 20)})
                except protocol.ToolRefused as error:
                    raise HubError(400, str(error)) from None
                result = await run_in_threadpool(search_transcript, ident, secret, query, limit)
            else:
                args = transcript_params(request)
                result = await run_in_threadpool(lambda: read_transcript(ident, secret, **args))
            return JSONResponse(result, headers={"cache-control": "no-store"})
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)

    class ShareTools:
        """What the share's MCP tools reach: this share alone, through the same checks as the guest page."""

        def __init__(self, ident, secret):
            self.ident, self.secret = ident, secret

        async def send(self, text) -> int:
            return (await run_in_threadpool(hub.guest_send, self.ident, self.secret, text))["seq"]

        async def view(self, after: int) -> dict:
            return await run_in_threadpool(hub.guest_view, self.ident, self.secret, after)

        async def wait(self, after: int, seconds: float) -> dict:
            return await wait_reply(self.ident, self.secret, after, seconds)

        async def read(self, **args) -> dict:
            return await run_in_threadpool(lambda: read_transcript(self.ident, self.secret, **args))

        async def search(self, query: str, limit: int) -> dict:
            return await run_in_threadpool(search_transcript, self.ident, self.secret, query, limit)

        async def archived_through(self) -> float | None:
            return await run_in_threadpool(archived_through, self.ident, self.secret)

    async def share_mcp(request):
        # The share's secret is the bearer token: an agent's MCP client sends it on every request.
        from . import share_mcp as protocol
        try:
            values = request.headers.getlist("authorization")
            scheme, _, secret = (values[0] if len(values) == 1 else "").partition(" ")
            if scheme.lower() != "bearer" or not secret:
                raise HubError(401, "this share's MCP server takes its secret as `Authorization: Bearer <secret>`")
            tools = ShareTools(request.path_params["share"], secret)
            # Checked before any message, so a dead link fails at connect rather than at the first tool.
            await tools.view(0)
            body = json.loads(await bounded_body(request, GUEST_BODY) or b"null")
        except HubError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status,
                                headers={"www-authenticate": "Bearer"} if error.status == 401 else None)
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                                status_code=400)
        messages = body if isinstance(body, list) else [body]
        answers = [reply for reply in [await protocol.answer(m, tools, (HubError,)) for m in messages] if reply is not None]
        if not answers:
            return Response(status_code=202)
        return JSONResponse(answers if isinstance(body, list) else answers[0])

    routes = [
        Route("/v1/whoami", whoami, methods=["GET"]),
        Route("/v1/shares", shares, methods=["GET", "POST"]),
        Route("/v1/shares/{share}", share, methods=["DELETE"]),
        Route("/v1/shares/{share}/messages", guest, methods=["GET", "POST"]),
        Route("/v1/shares/{share}/transcript", transcript, methods=["GET"]),
        Route("/v1/shares/{share}/transcript/search", transcript, methods=["GET"]),
        Route("/s/{share}", page, methods=["GET"]),
        Route("/s/{share}/mcp", share_mcp, methods=["POST"]),
        Route("/v1/sessions", sessions, methods=["GET", "PUT"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/messages/{msg_id}", status, methods=["GET"]),
        Route("/v1/inbox", inbox, methods=["GET"]),
        Route("/v1/acks", acks, methods=["POST"]),
        Route("/v1/attachments", upload, methods=["POST"]),
        Route("/v1/attachments/{ident}", attachment, methods=["GET"]),
        Route("/v1/user", user_notes, methods=["GET"]),
        Route("/v1/board", board_topics, methods=["GET"]),
        Route("/v1/board/search", board_search, methods=["GET"]),
        Route("/v1/board/{topic}", board_topic, methods=["GET", "POST"]),
        Route("/v1/board/{topic}/files", board_upload, methods=["POST"]),
        Route("/v1/board/{topic}/files/{ident}", board_file, methods=["GET"]),
        Route("/v1/board/{topic}/posts/{ident}", board_delete, methods=["DELETE"]),
        Route("/v1/invites", invites, methods=["POST"]),
        Route("/v1/join", join, methods=["POST"]),
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
