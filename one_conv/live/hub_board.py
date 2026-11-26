"""The board on the Python relay: notes on shared topics, read later by whoever needs them.

A topic is a slug someone chose; it exists from its first post. A post is kept once under its
client-chosen id with a digest of its payload, so a retry returns the first receipt and a
different payload under the same id is a conflict. Posting wakes nobody: readers pull, and each
reader keeps its own read position on its own machine.

A file is uploaded on its own first (POST /v1/board/<topic>/files, the raw bytes), then named
by id in the post that carries it. Its bytes live on the filesystem next to the relay's
database under `board/<topic>/<file id>`; its name, size, content type and sha256 in SQLite.
A file is served only while a live post carries it.

relay-worker/src/board.ts is the same board on the Cloudflare Worker.
"""

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import unquote

from .hub import MESSAGE_ID, NAME_LIMIT, TAG, TAGS_PER_NOTE, HubError

TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# `search` is a route under /v1/board, so no topic can take its name.
RESERVED = ("search",)
FILE_ID = re.compile(r"[0-9a-f]{32}")
TEXT_LIMIT = 32 * 1024
FILE_LIMIT = 25_000_000
FILES_PER_POST = 4
FILE_NAME_LIMIT = 255
CONTENT_TYPE = re.compile(r"[\x21-\x7e]+/[\x20-\x7e]+")
CONTENT_TYPE_LIMIT = 128
DEFAULT_TYPE = "application/octet-stream"
TITLE_LIMIT = 200
READ_LIMIT = 200
SEARCH_LIMIT = 200
QUERY_LIMIT = 1000
SEARCH_WORDS = 8
SCHEMA = """
CREATE TABLE IF NOT EXISTS board_topics (slug TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL,
    last_post REAL NOT NULL, posts INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS board_posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, id TEXT NOT NULL UNIQUE,
    from_node TEXT NOT NULL, digest TEXT NOT NULL, author TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL,
    tags TEXT NOT NULL, created REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, attachments TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS board_topic_posts ON board_posts (topic, seq);
CREATE TABLE IF NOT EXISTS board_files (id TEXT PRIMARY KEY, topic TEXT NOT NULL, node TEXT NOT NULL, name TEXT NOT NULL,
    size INTEGER NOT NULL, content_type TEXT NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL, post TEXT);
"""
POST_COLUMNS = "seq, topic, id, from_node, author, name, text, tags, created, attachments"


def check_topic(topic) -> str:
    if not isinstance(topic, str) or not TOPIC.fullmatch(topic) or topic in RESERVED:
        raise HubError(400, "a topic is 1 to 48 lowercase letters, digits and dashes, starting with a letter or digit"
                            f" (not {', '.join(RESERVED)})")
    return topic


def check_tags(tags) -> list[str]:
    if (not isinstance(tags, list) or len(tags) > TAGS_PER_NOTE
            or not all(isinstance(t, str) and TAG.fullmatch(t) for t in tags)):
        raise HubError(400, f"tags is a list of at most {TAGS_PER_NOTE} lowercase words (letters, digits, dashes)")
    return list(dict.fromkeys(tags))


def file_name(raw: str) -> str:
    """The uploaded file's name from its percent-encoded header: one path segment, printable."""
    try:
        name = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        raise HubError(400, "x-file-name is the percent-encoded UTF-8 file name") from None
    if (not name or len(name) > FILE_NAME_LIMIT or name in (".", "..") or "/" in name or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise HubError(400, f"a file name is 1 to {FILE_NAME_LIMIT} printable characters without slashes")
    return name


def content_type(raw: str) -> str:
    raw = raw.strip()
    return raw if raw and len(raw) <= CONTENT_TYPE_LIMIT and CONTENT_TYPE.fullmatch(raw) else DEFAULT_TYPE


def _post(row) -> dict:
    seq, topic, ident, from_node, author, name, text, tags, created, attachments = row
    return {"seq": seq, "topic": topic, "id": ident, "from": f"{from_node}/{author}", "name": name, "text": text,
            "tags": json.loads(tags), "created": created, "files": json.loads(attachments)}


def _like(word: str) -> str:
    """A LIKE pattern matching `word` anywhere, its wildcards taken literally."""
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Board:
    """The board's tables in the relay's database, and its files beside it."""

    def __init__(self, hub):
        self.hub = hub
        self.root = Path(hub.path).resolve().parent / "board"

    def _path(self, topic: str, ident: str) -> Path:
        return self.root / topic / ident

    def topics(self, reads: dict[str, int]) -> dict:
        """Every topic with a live post, most recently posted first; `unread` counts live posts after the reader's position."""
        with self.hub._db() as db:
            rows = db.execute("SELECT slug, title, created, last_post, posts, archived,"
                              " (SELECT COALESCE(MAX(seq), 0) FROM board_posts WHERE topic = slug)"
                              " FROM board_topics WHERE posts > 0 ORDER BY last_post DESC, slug").fetchall()
            out = []
            for slug, title, created, last_post, posts, archived, last in rows:
                unread = db.execute("SELECT COUNT(*) FROM board_posts WHERE topic = ? AND seq > ? AND deleted = 0",
                                    (slug, reads.get(slug, 0))).fetchone()[0]
                out.append({"topic": slug, "title": title, "created": created, "last_post": last_post,
                            "posts": posts, "last": last, "archived": bool(archived), "unread": unread})
        return {"topics": out}

    def read(self, topic: str, after: int, tags: list[str], limit: int) -> dict:
        """Live posts after `after`, oldest first; `last` is the topic's newest seq, past anything the filter skipped."""
        check_topic(topic)
        # A post carries at most TAGS_PER_NOTE tags, which also keeps the query under SQLite's parameter cap.
        if len(tags) > TAGS_PER_NOTE or not all(TAG.fullmatch(t) for t in tags):
            raise HubError(400, f"at most {TAGS_PER_NOTE} tags, each lowercase letters, digits and dashes")
        limit = max(1, min(limit, READ_LIMIT))
        tag_filter = (f" AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value IN ({', '.join('?' * len(tags))}))"
                      if tags else "")
        with self.hub._db() as db:
            found = db.execute("SELECT title FROM board_topics WHERE slug = ?", (topic,)).fetchone()
            if found is None:
                raise HubError(404, f"no topic named {topic}")
            rows = db.execute(f"SELECT {POST_COLUMNS} FROM board_posts WHERE topic = ? AND seq > ? AND deleted = 0"
                              f"{tag_filter} ORDER BY seq LIMIT ?", (topic, max(0, after), *tags, limit)).fetchall()
            last = db.execute("SELECT COALESCE(MAX(seq), 0) FROM board_posts WHERE topic = ?", (topic,)).fetchone()[0]
        return {"topic": topic, "title": found[0], "posts": [_post(row) for row in rows], "last": last}

    def upload(self, node: str, topic: str, name: str, media_type: str, body: bytes) -> dict:
        """Keep one file for a post to come; its id goes into that post's `files`."""
        check_topic(topic)
        if not body or len(body) > FILE_LIMIT:
            raise HubError(413, f"a file is 1 byte to {FILE_LIMIT // 1_000_000} MB")
        ident, digest, kind = secrets.token_hex(16), hashlib.sha256(body).hexdigest(), content_type(media_type)
        path = self._path(topic, ident)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{ident}.tmp")
        partial.write_bytes(body)
        # The bytes are durable before the id exists, so a listed file always has them.
        os.replace(partial, path)
        with self.hub.lock, self.hub._db() as db:
            db.execute("INSERT INTO board_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                       (ident, topic, node, name, len(body), kind, digest, time.time()))
        return {"id": ident, "name": name, "size": len(body), "content_type": kind, "sha256": digest}

    def post(self, node: str, topic: str, body: dict) -> dict:
        """Store a post, creating its topic; a retry under the same id returns the first receipt."""
        check_topic(topic)
        ident, sender, text = body.get("id"), body.get("from"), body.get("text", "")
        if not isinstance(ident, str) or not MESSAGE_ID.fullmatch(ident):
            raise HubError(400, "id must be 32 lowercase hex characters, chosen by the poster")
        files = body.get("files") or []
        if (not isinstance(files, list) or len(files) > FILES_PER_POST
                or not all(isinstance(f, str) and FILE_ID.fullmatch(f) for f in files) or len(set(files)) != len(files)):
            raise HubError(400, f"files is a list of at most {FILES_PER_POST} distinct uploaded file ids")
        if not isinstance(text, str) or (not text and not files):
            raise HubError(400, "text must be a string, non-empty unless a file is attached")
        try:
            size = len(text.encode())
        except UnicodeEncodeError:
            raise HubError(400, "text must be valid Unicode") from None
        if size > TEXT_LIMIT:
            raise HubError(413, f"a post's text is at most {TEXT_LIMIT} bytes")
        tags = check_tags(body.get("tags") or [])
        name, title = body.get("name") or "", body.get("title") or ""
        if not isinstance(name, str) or len(name) > NAME_LIMIT:
            raise HubError(400, f"name is a string of at most {NAME_LIMIT} characters")
        if not isinstance(title, str) or len(title) > TITLE_LIMIT:
            raise HubError(400, f"title is a string of at most {TITLE_LIMIT} characters")
        with self.hub.lock, self.hub._db() as db:
            db.execute("BEGIN IMMEDIATE")
            attached = []
            for file_id in files:
                row = db.execute("SELECT name, size, content_type, sha256, node, topic, post FROM board_files WHERE id = ?",
                                 (file_id,)).fetchone()
                if row is None or row[4] != node or row[5] != topic or row[6] not in (None, ident):
                    raise HubError(400, f"file {file_id} was not uploaded by this node to {topic}, or is attached elsewhere")
                attached.append({"id": file_id, "name": row[0], "size": row[1], "content_type": row[2], "sha256": row[3]})
            # File content, not file ids, joins the digest: a retry that uploads the same files again is the same post.
            digest = hashlib.sha256(json.dumps([topic, sender, name, title, text, tags,
                                                [[f["name"], f["sha256"]] for f in attached]]).encode()).hexdigest()
            existing = db.execute("SELECT seq, from_node, digest, created FROM board_posts WHERE id = ?", (ident,)).fetchone()
            if existing and (existing[1] != node or existing[2] != digest):
                raise HubError(409, "this id was already used for a different post; post it under a new id")
            if existing:
                seq, created = existing[0], existing[3]
            else:
                if not isinstance(sender, str) or (not self.hub._published(db, node, sender)
                                                   and sender not in ("user", "unknown")):
                    raise HubError(403, "from must be a session this node published, or user/unknown")
                created = time.time()
                seq = db.execute("INSERT INTO board_posts (topic, id, from_node, digest, author, name, text, tags, created,"
                                 " attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (topic, ident, node, digest, sender, name, text, json.dumps(tags), created,
                                  json.dumps(attached))).lastrowid
                for file_id in files:
                    db.execute("UPDATE board_files SET post = ? WHERE id = ?", (ident, file_id))
                db.execute("INSERT INTO board_topics VALUES (?, ?, ?, ?, 1, 0) ON CONFLICT (slug) DO UPDATE SET"
                           " last_post = excluded.last_post, posts = posts + 1,"
                           " title = CASE WHEN excluded.title != '' THEN excluded.title ELSE title END",
                           (topic, title, created, created))
            previous = db.execute("SELECT COALESCE(MAX(seq), 0) FROM board_posts WHERE topic = ? AND seq < ?",
                                  (topic, seq)).fetchone()[0]
        return {"id": ident, "topic": topic, "seq": seq, "previous": previous, "created": created,
                "duplicate": bool(existing)}

    def file(self, topic: str, ident: str) -> tuple[dict, bytes]:
        """A file a live post carries, with its metadata."""
        check_topic(topic)
        if not FILE_ID.fullmatch(ident):
            raise HubError(400, "a file id is 32 lowercase hex characters")
        with self.hub._db() as db:
            row = db.execute("SELECT f.name, f.size, f.content_type, f.sha256 FROM board_files f"
                             " JOIN board_posts p ON p.id = f.post WHERE f.id = ? AND f.topic = ? AND p.deleted = 0",
                             (ident, topic)).fetchone()
        if row is None:
            raise HubError(404, "no such file on this topic")
        try:
            data = self._path(topic, ident).read_bytes()
        except FileNotFoundError:
            raise HubError(404, "no such file on this topic") from None
        return {"id": ident, "name": row[0], "size": row[1], "content_type": row[2], "sha256": row[3]}, data

    def delete(self, node: str, topic: str, ident: str, admin: bool = False) -> dict:
        """Hide a post from every read; only the node that posted it, or the relay's operator, may."""
        check_topic(topic)
        with self.hub.lock, self.hub._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT from_node, deleted FROM board_posts WHERE id = ? AND topic = ?",
                             (ident, topic)).fetchone()
            if row is None:
                raise HubError(404, "no such post on this topic")
            if row[0] != node and not admin:
                raise HubError(403, "only the node that posted it, or the relay's admin, deletes a post")
            if not row[1]:
                db.execute("UPDATE board_posts SET deleted = 1 WHERE id = ?", (ident,))
                db.execute("UPDATE board_topics SET posts = posts - 1 WHERE slug = ?", (topic,))
        return {"id": ident, "topic": topic, "deleted": True}

    def search(self, query: str, topic: str | None, words: bool, limit: int) -> dict:
        """Live posts whose text holds `query` (or, with `words`, each of its words), newest first, ignoring ASCII case."""
        if not isinstance(query, str) or not query.strip() or len(query) > QUERY_LIMIT:
            raise HubError(400, f"q is 1 to {QUERY_LIMIT} characters")
        if topic:
            check_topic(topic)
        terms = list(dict.fromkeys(query.split())) if words else [query]
        if len(terms) > SEARCH_WORDS:
            raise HubError(400, f"an all-words search takes at most {SEARCH_WORDS} words")
        limit = max(1, min(limit, SEARCH_LIMIT))
        where = " AND ".join(["text LIKE ? ESCAPE '\\'"] * len(terms)) + (" AND topic = ?" if topic else "")
        with self.hub._db() as db:
            rows = db.execute(f"SELECT {POST_COLUMNS} FROM board_posts WHERE deleted = 0 AND {where}"
                              " ORDER BY seq DESC LIMIT ?", (*map(_like, terms), *([topic] if topic else []), limit)).fetchall()
        return {"posts": [_post(row) for row in rows]}


def parse_reads(values: list[str]) -> dict[str, int]:
    """`read=<topic>:<seq>` query values: a reader's positions, for unread counts."""
    reads = {}
    for value in values:
        topic, _, seq = value.rpartition(":")
        if not TOPIC.fullmatch(topic) or not re.fullmatch(r"[0-9]{1,15}", seq):
            raise HubError(400, "read is <topic>:<seq>")
        reads[topic] = int(seq)
    return reads
