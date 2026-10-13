"""Conversation storage contract and SQLite implementation using SQLAlchemy Core."""

import hashlib
import json
from typing import Protocol

from sqlalchemy import Column, Float, LargeBinary, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert

from one_conv.archive import conversation_key, encode, revision


class Conflict(Exception):
    pass


class ConversationStore(Protocol):
    def list_conversations(self, after: str, query: str | None, limit: int) -> dict: ...
    def read_conversation(self, key: str, revision: str | None = None) -> dict | None: ...
    def put(self, document: dict, expected: str | None) -> dict: ...


CHUNK_BYTES = 64 * 1024
CAPTURE_FIELDS = ("turns", "turns_with_subagents", "search_text")


class SQLiteStore:
    """Immutable revisions retain earlier captures; a CAS advances the visible head."""

    def __init__(self, url):
        self.engine = create_engine(url)
        if self.engine.dialect.name != "sqlite":
            raise ValueError("Only SQLite is verified; add another ConversationStore implementation for other engines.")
        schema = MetaData()
        self.heads = Table("conversations_v1", schema,
            Column("key", String, primary_key=True), Column("revision", String, nullable=False),
            Column("mtime", Float, nullable=False), Column("metadata", Text, nullable=False),
            Column("search_text", Text, nullable=False))
        self.revisions = Table("revisions_v1", schema,
            Column("key", String, primary_key=True), Column("revision", String, primary_key=True),
            Column("document", Text, nullable=False))
        self.chunks = Table("chunks_v1", schema,
            Column("digest", String, primary_key=True), Column("data", LargeBinary, nullable=False))
        schema.create_all(self.engine)

    def list_conversations(self, after="", query=None, limit=100):
        h = self.heads
        statement = select(h.c.key, h.c.metadata, h.c.revision).where(h.c.key > after).order_by(h.c.key)
        if query is not None:
            # Python lower() is applied on both sides; SQL wildcard characters are literal.
            statement = statement.where(h.c.search_text.contains(query.lower(), autoescape=True))
        with self.engine.connect() as conn:
            rows = conn.execute(statement.limit(limit + 1)).all()
        return {"items": [{**json.loads(row.metadata), "key": row.key, "revision": row.revision} for row in rows[:limit]],
                "next": rows[limit - 1].key if len(rows) > limit else None}

    def read_conversation(self, key, revision=None):
        h, r = self.heads, self.revisions
        with self.engine.connect() as conn:
            statement = (select(r.c.document).where((r.c.key == key) & (r.c.revision == revision)) if revision else
                         select(r.c.document).join(h, (h.c.key == r.c.key) & (h.c.revision == r.c.revision)).where(h.c.key == key))
            row = conn.execute(statement).first()
            return self._unpack(conn, json.loads(row[0])) if row else None

    def _pack(self, conn, document):
        pending = {}
        def chunks(value):
            payload = encode(value)
            digests = []
            for start in range(0, len(payload), CHUNK_BYTES):
                data = payload[start:start + CHUNK_BYTES]
                digest = hashlib.sha256(data).hexdigest()
                pending[digest] = data
                digests.append(digest)
            return digests

        # Chunk fields separately so appended turns do not shift original-record chunks.
        manifest = {k: v for k, v in document.items() if k not in (*CAPTURE_FIELDS, "originals")}
        manifest["_chunks"] = {field: chunks(document[field]) for field in CAPTURE_FIELDS}
        manifest["_original_chunks"] = {path: chunks(value) for path, value in document["originals"].items()}
        conn.execute(insert(self.chunks).on_conflict_do_nothing(),
            [{"digest": digest, "data": data} for digest, data in pending.items()])
        return manifest

    def _unpack(self, conn, document):
        if "_chunks" not in document:
            return document  # Captures written before chunk storage remain readable.
        fields = document.pop("_chunks")
        originals = document.pop("_original_chunks")
        digests = list({digest for parts in (*fields.values(), *originals.values()) for digest in parts})
        data = {}
        for start in range(0, len(digests), 500):
            data.update(conn.execute(select(self.chunks.c.digest, self.chunks.c.data).where(
                self.chunks.c.digest.in_(digests[start:start + 500]))).all())
        def restore(parts):
            return json.loads(b"".join(data[digest] for digest in parts))
        document.update({field: restore(parts) for field, parts in fields.items()})
        document["originals"] = {path: restore(parts) for path, parts in originals.items()}
        return document

    def put(self, document, expected):
        key = conversation_key(document["origin"], document["source"], document["cwd"], document["session"])
        digest = revision(document)
        metadata = {name: document[name] for name in ("origin", "source", "cwd", "session", "mtime", "summary", "unread_native")}
        values = {"revision": digest, "mtime": document["mtime"], "metadata": encode(metadata).decode(),
                  "search_text": "\n".join(document["search_text"]).lower()}
        h, r = self.heads, self.revisions
        try:
            with self.engine.begin() as conn:
                current = conn.execute(select(h.c.revision, h.c.mtime).where(h.c.key == key)).first()
                if current and current.revision == digest:
                    return {"key": key, "revision": digest}
                if (current.revision if current else None) != expected or (current and current.mtime > document["mtime"]):
                    raise Conflict("Conversation changed; resync from its current source.")
                if current:
                    previous = self._unpack(conn, json.loads(conn.execute(select(r.c.document).where(
                        (r.c.key == key) & (r.c.revision == current.revision))).scalar_one()))
                    for field in ("turns", "turns_with_subagents"):
                        old = previous[field] if previous[field] is not None else previous["turns"]
                        new = document[field] if document[field] is not None else document["turns"]
                        if new[:len(old)] != old:
                            raise Conflict("Capture rewrites archived messages; keeping the previous capture.")
                    result = conn.execute(h.update().where((h.c.key == key) & (h.c.revision == expected)).values(**values))
                    if result.rowcount != 1:
                        raise Conflict("Concurrent sync; retry after reading the current revision.")
                else:
                    conn.execute(h.insert().values(key=key, **values))
                if not conn.execute(select(r.c.key).where((r.c.key == key) & (r.c.revision == digest))).first():
                    conn.execute(r.insert().values(key=key, revision=digest, document=encode(self._pack(conn, document)).decode()))
        except IntegrityError as error:
            raise Conflict("Concurrent sync; retry.") from error
        return {"key": key, "revision": digest}
