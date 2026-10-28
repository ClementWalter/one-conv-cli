"""Rank search hits by relevance with a persistent SQLite index of every searched session's turns.

Words go to an FTS5 table ranked by BM25; semantic search adds one embedding per turn, fused with BM25.
"""

import re
import sqlite3
from pathlib import Path

# Accents fold away inside the index, so a query "café" also matches "cafe".
TOKENIZER = "unicode61 remove_diacritics 2"
SNIPPET_CHARS = 200
# Bump when the schema, the indexed text or the embedding model changes: an index of another version is rebuilt.
SCHEMA_VERSION = 3
# Reciprocal rank fusion: score = sum of weight / (FUSION_K + rank). Tuned on reply-retrieval proxies of real
# history, where BM25 over any word beats the static embeddings alone and their fusion beats both.
FUSION_K = 10
LEXICAL_WEIGHT, SEMANTIC_WEIGHT = 1.0, 0.5
FUSION_DEPTH = 100


def words(text):
    """The lowercase words of a query, in order and without repeats."""
    return list(dict.fromkeys(re.findall(r"\w+", text.lower())))


def snippet(text, needles):
    """One line of `text` of at most SNIPPET_CHARS, centred on the first needle found."""
    line = text.replace("\n", " ")
    if len(line) <= SNIPPET_CHARS:
        return line
    lowered = line.lower()
    found = [index for index in (lowered.find(needle) for needle in needles) if index >= 0]
    start = max(0, min(found, default=0) - 80)
    return ("…" if start else "") + line[start:start + SNIPPET_CHARS] + "…"


class SearchIndex:
    """Turns of sessions keyed by an opaque `key`; a session is re-indexed only when its `stamp` changes.

    Several commands may search at once, so writes wait for the lock instead of failing.
    """

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=120, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                for table in ("sessions", "turns", "turn_text", "turn_vectors"):
                    self.db.execute(f"DROP TABLE IF EXISTS {table}")
                self.db.execute("CREATE TABLE sessions(key TEXT PRIMARY KEY, source TEXT NOT NULL, stamp TEXT NOT NULL)")
                self.db.execute("CREATE TABLE turns(id INTEGER PRIMARY KEY, key TEXT NOT NULL, ts TEXT, role TEXT)")
                self.db.execute("CREATE INDEX turns_key ON turns(key)")
                self.db.execute(f"CREATE VIRTUAL TABLE turn_text USING fts5(text, tokenize='{TOKENIZER}')")
                # Filled only by semantic searches, so word-only searches never load the model.
                self.db.execute("CREATE TABLE turn_vectors(id INTEGER PRIMARY KEY, vector BLOB NOT NULL)")
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def stamps(self):
        return dict(self.db.execute("SELECT key, stamp FROM sessions"))

    def replace(self, key, source, stamp, turns):
        """Store `turns`, (ts, role, text) tuples, as the whole content of session `key` at `stamp`."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._delete(key)
            for ts, role, text in turns:
                rowid = self.db.execute("INSERT INTO turns(key, ts, role) VALUES (?, ?, ?)", (key, ts, role)).lastrowid
                self.db.execute("INSERT INTO turn_text(rowid, text) VALUES (?, ?)", (rowid, text))
            self.db.execute("INSERT INTO sessions(key, source, stamp) VALUES (?, ?, ?)", (key, source, stamp))

    def forget(self, sources, present):
        """Drop the sessions of `sources` missing from `present`, every existing session of those sources."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" * len(sources))
            for (key,) in self.db.execute(f"SELECT key FROM sessions WHERE source IN ({marks})", tuple(sources)).fetchall():
                if key not in present:
                    self._delete(key)

    def _delete(self, key):
        self.db.execute("DELETE FROM turn_vectors WHERE id IN (SELECT id FROM turns WHERE key = ?)", (key,))
        self.db.execute("DELETE FROM turn_text WHERE rowid IN (SELECT id FROM turns WHERE key = ?)", (key,))
        self.db.execute("DELETE FROM turns WHERE key = ?", (key,))
        self.db.execute("DELETE FROM sessions WHERE key = ?", (key,))

    def _scope(self, keys):
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS scope(key TEXT PRIMARY KEY)")
        self.db.execute("DELETE FROM temp.scope")
        self.db.executemany("INSERT OR IGNORE INTO temp.scope(key) VALUES (?)", ((key,) for key in keys))

    def _lexical(self, terms, joiner, limit):
        """Turn ids of the scope matching `terms` joined by `joiner`, best BM25 first."""
        # Quoted terms are literal: `\w+` words hold no quote, so FTS5 operators never apply.
        # The trailing `*` lets "build" match "builds".
        expression = joiner.join(f'"{term}"*' for term in terms)
        return [row[0] for row in self.db.execute(
            "SELECT turns.id FROM turn_text JOIN turns ON turns.id = turn_text.rowid"
            " WHERE turn_text MATCH ? AND turns.key IN (SELECT key FROM temp.scope)"
            " ORDER BY bm25(turn_text), turns.ts DESC LIMIT ?", (expression, limit))]

    def _rows(self, ids):
        """(key, ts, role, text) of turn `ids`, in their order."""
        marks = ",".join("?" * len(ids))
        found = {row[0]: row[1:] for row in self.db.execute(
            "SELECT turns.id, turns.key, turns.ts, turns.role, turn_text.text FROM turns"
            f" JOIN turn_text ON turn_text.rowid = turns.id WHERE turns.id IN ({marks})", ids)}
        return [found[turn] for turn in ids]

    def top(self, terms, keys, limit):
        """(key, ts, role, text) of the turns of sessions in `keys` holding every term, best first."""
        self._scope(keys)
        return self._rows(self._lexical(terms, " ", limit))

    def unembedded(self, keys):
        """(id, text) of the turns of sessions in `keys` that have no embedding yet."""
        self._scope(keys)
        return self.db.execute(
            "SELECT turns.id, turn_text.text FROM turns JOIN turn_text ON turn_text.rowid = turns.id"
            " WHERE turns.key IN (SELECT key FROM temp.scope)"
            " AND turns.id NOT IN (SELECT id FROM turn_vectors)").fetchall()

    def embed(self, ids, vectors):
        """Store row i of `vectors` as the embedding of turn ids[i]; half precision halves the index."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.executemany("INSERT OR REPLACE INTO turn_vectors(id, vector) VALUES (?, ?)",
                                zip(ids, (row.tobytes() for row in vectors.astype("<f2"))))

    def _nearest(self, vector, limit):
        """Turn ids of the scope whose embedding is closest to `vector`, best first."""
        import numpy as np
        rows = self.db.execute("SELECT turn_vectors.id, turn_vectors.vector FROM turn_vectors"
                               " JOIN turns ON turns.id = turn_vectors.id"
                               " WHERE turns.key IN (SELECT key FROM temp.scope)").fetchall()
        if not rows or not vector.any():
            return []
        matrix = np.frombuffer(b"".join(row[1] for row in rows), dtype="<f2").reshape(len(rows), -1)
        scores = matrix.astype(np.float32) @ vector
        best = np.argsort(-scores, kind="stable")[:limit]
        return [rows[i][0] for i in best if scores[i] > 0]

    def hybrid(self, terms, vector, keys, limit):
        """(key, ts, role, text) of the turns of sessions in `keys` that best match any term or `vector`.

        BM25 over turns holding any term and cosine similarity of embeddings each rank FUSION_DEPTH turns;
        reciprocal rank fusion merges the two lists, so a turn need not share a word with the query.
        """
        self._scope(keys)
        depth = max(FUSION_DEPTH, limit)
        scores = {}
        for ranked, weight in ((self._lexical(terms, " OR ", depth) if terms else [], LEXICAL_WEIGHT),
                               (self._nearest(vector, depth), SEMANTIC_WEIGHT)):
            for rank, turn in enumerate(ranked):
                scores[turn] = scores.get(turn, 0.0) + weight / (FUSION_K + rank + 1)
        return self._rows(sorted(scores, key=lambda turn: -scores[turn])[:limit])
