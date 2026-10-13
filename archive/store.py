"""Conversation storage contract and SQLite implementation using SQLAlchemy Core."""

import json
from typing import Protocol

from sqlalchemy import Column, Float, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.exc import IntegrityError

from one_conv.archive import conversation_key, encode, revision


class Conflict(Exception):
    pass


class ConversationStore(Protocol):
    def list_conversations(self, after: str, query: str | None, limit: int) -> dict: ...
    def read_conversation(self, key: str, revision: str | None = None) -> dict | None: ...
    def put(self, document: dict, expected: str | None) -> dict: ...


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
        return json.loads(row[0]) if row else None

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
                    previous = json.loads(conn.execute(select(r.c.document).where(
                        (r.c.key == key) & (r.c.revision == current.revision))).scalar_one())
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
                    conn.execute(r.insert().values(key=key, revision=digest, document=encode(document).decode()))
        except IntegrityError as error:
            raise Conflict("Concurrent sync; retry.") from error
        return {"key": key, "revision": digest}
