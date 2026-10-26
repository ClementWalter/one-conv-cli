"""Rank search hits by relevance with a persistent SQLite FTS5 index of every searched session's turns."""

import re
import sqlite3
from pathlib import Path

# Accents fold away inside the index, so a query "café" also matches "cafe".
TOKENIZER = "unicode61 remove_diacritics 2"
SNIPPET_CHARS = 200
# Bump when the schema or the indexed text changes: an index of another version is rebuilt.
SCHEMA_VERSION = 2


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
                for table in ("sessions", "turns", "turn_text"):
                    self.db.execute(f"DROP TABLE IF EXISTS {table}")
                self.db.execute("CREATE TABLE sessions(key TEXT PRIMARY KEY, source TEXT NOT NULL, stamp TEXT NOT NULL)")
                self.db.execute("CREATE TABLE turns(id INTEGER PRIMARY KEY, key TEXT NOT NULL, ts TEXT, role TEXT)")
                self.db.execute("CREATE INDEX turns_key ON turns(key)")
                self.db.execute(f"CREATE VIRTUAL TABLE turn_text USING fts5(text, tokenize='{TOKENIZER}')")
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
        self.db.execute("DELETE FROM turn_text WHERE rowid IN (SELECT id FROM turns WHERE key = ?)", (key,))
        self.db.execute("DELETE FROM turns WHERE key = ?", (key,))
        self.db.execute("DELETE FROM sessions WHERE key = ?", (key,))

    def top(self, terms, keys, limit):
        """(key, ts, role, text) of the turns of sessions in `keys` holding every term, best first."""
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS scope(key TEXT PRIMARY KEY)")
        self.db.execute("DELETE FROM temp.scope")
        self.db.executemany("INSERT OR IGNORE INTO temp.scope(key) VALUES (?)", ((key,) for key in keys))
        # Quoted terms are literal: `\w+` words hold no quote, so FTS5 operators never apply.
        # The trailing `*` lets "build" match "builds".
        expression = " ".join(f'"{term}"*' for term in terms)
        return self.db.execute(
            "SELECT turns.key, turns.ts, turns.role, turn_text.text FROM turn_text JOIN turns ON turns.id = turn_text.rowid"
            " WHERE turn_text MATCH ? AND turns.key IN (SELECT key FROM temp.scope)"
            " ORDER BY bm25(turn_text), turns.ts DESC LIMIT ?", (expression, limit)).fetchall()
