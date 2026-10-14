"""Conversation storage contract and SQLite implementation using SQLAlchemy Core."""

import json
from typing import Protocol

from sqlalchemy import Column, Float, LargeBinary, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert

from one_conv.archive import encode


class Conflict(Exception):
    pass


class ConversationStore(Protocol):
    def list_conversations(self, after: str, query: str | None, limit: int) -> dict: ...
    def read_conversation(self, key: str, revision: str | None = None) -> dict | None: ...
    def missing_chunks(self, digests: list[str]) -> list[str]: ...
    def put_chunks(self, chunks: dict[str, bytes]) -> None: ...
    def read_chunk(self, digest: str) -> bytes | None: ...
    def put(self, key: str, revision: str, metadata: dict, search_text: str, manifest: dict, expected: str | None) -> dict: ...


class SQLiteStore:
    """Immutable manifests and compressed chunks; a CAS advances the visible head."""

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
            Column("manifest", Text, nullable=False))
        self.chunks = Table("chunks_v1", schema,
            Column("digest", String, primary_key=True), Column("data", LargeBinary, nullable=False))
        schema.create_all(self.engine)

    def list_conversations(self, after="", query=None, limit=1000):
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
        statement = (select(r.c.manifest).where((r.c.key == key) & (r.c.revision == revision)) if revision else
                     select(r.c.manifest).join(h, (h.c.key == r.c.key) & (h.c.revision == r.c.revision)).where(h.c.key == key))
        with self.engine.connect() as conn:
            row = conn.execute(statement).first()
        return json.loads(row.manifest) if row else None

    def missing_chunks(self, digests):
        present = set()
        with self.engine.connect() as conn:
            for start in range(0, len(digests), 500):
                present.update(conn.execute(select(self.chunks.c.digest).where(
                    self.chunks.c.digest.in_(digests[start:start + 500]))).scalars())
        return [d for d in digests if d not in present]

    def put_chunks(self, chunks):
        with self.engine.begin() as conn:
            conn.execute(insert(self.chunks).on_conflict_do_nothing(),
                         [{"digest": d, "data": data} for d, data in chunks.items()])

    def read_chunk(self, digest):
        with self.engine.connect() as conn:
            return conn.execute(select(self.chunks.c.data).where(self.chunks.c.digest == digest)).scalar()

    def put(self, key, revision, metadata, search_text, manifest, expected):
        values = {"revision": revision, "mtime": metadata["mtime"], "metadata": encode(metadata).decode(), "search_text": search_text}
        h, r = self.heads, self.revisions
        try:
            with self.engine.begin() as conn:
                current = conn.execute(select(h.c.revision, h.c.mtime).where(h.c.key == key)).first()
                if current and current.revision == revision:
                    return {"key": key, "revision": revision}
                if (current.revision if current else None) != expected or (current and current.mtime > metadata["mtime"]):
                    raise Conflict("Conversation changed; resync from its current source.")
                if current:
                    result = conn.execute(h.update().where((h.c.key == key) & (h.c.revision == expected)).values(**values))
                    if result.rowcount != 1:
                        raise Conflict("Concurrent sync; retry after reading the current revision.")
                else:
                    conn.execute(h.insert().values(key=key, **values))
                if not conn.execute(select(r.c.key).where((r.c.key == key) & (r.c.revision == revision))).first():
                    conn.execute(r.insert().values(key=key, revision=revision, manifest=encode(manifest).decode()))
        except IntegrityError as error:
            raise Conflict("Concurrent sync; retry.") from error
        return {"key": key, "revision": revision}
