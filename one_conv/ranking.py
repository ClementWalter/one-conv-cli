"""Rank search hits by relevance with an in-memory SQLite FTS5 index built over candidate turns."""

import re
import sqlite3

# Accents fold away inside the index, so a query "café" also matches "cafe".
TOKENIZER = "unicode61 remove_diacritics 2"
SNIPPET_CHARS = 200


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


class Ranking:
    """Turns added as (row, text); `top` returns the rows whose text holds every word, best first.

    Only the text lives in SQLite, so a large candidate set never sits in Python memory twice.
    """

    def __init__(self, terms):
        self.terms = terms
        self.rows = []
        self.db = sqlite3.connect(":memory:")
        self.db.execute(f"CREATE VIRTUAL TABLE turns USING fts5(text, ts UNINDEXED, tokenize='{TOKENIZER}')")

    def add(self, row, text):
        self.db.execute("INSERT INTO turns(rowid, text, ts) VALUES (?, ?, ?)", (len(self.rows), text, row.get("ts") or ""))
        self.rows.append(row)

    def top(self, limit):
        # Quoted terms are literal: `\w+` words hold no quote, so FTS5 operators never apply.
        # The trailing `*` lets "build" match "builds"; the raw-bytes prefilter still holds.
        expression = " ".join(f'"{term}"*' for term in self.terms)
        found = self.db.execute("SELECT rowid FROM turns WHERE turns MATCH ? ORDER BY rank, ts DESC LIMIT ?",
                                (expression, limit))
        return [self.rows[rowid] for (rowid,) in found]
