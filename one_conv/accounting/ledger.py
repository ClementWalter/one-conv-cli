"""A transactional SQLite ledger deduplicates provider requests across machines and workers."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .model import (
    AccountingConflict,
    AccountingError,
    comparable,
    normalize_event,
    timestamp,
)
from .summary import summarize


class Ledger:
    """Use a server-owned directory for shared authority; each write is one SQLite transaction."""

    def __init__(self, directory):
        self.path = Path(directory) / "usage.sqlite3"

    def record(self, event):
        return self.record_many([event])[0]

    def record_many(self, events):
        """A conflicting batch rolls back completely, so imports remain safely retryable."""
        events = [normalize_event(event) for event in events]
        if not events:
            return []
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise AccountingError("Unsupported accounting ledger schema")
                connection.execute("CREATE TABLE IF NOT EXISTS usage (event_id TEXT PRIMARY KEY, cost_center_id TEXT NOT NULL, stamp TEXT NOT NULL, payload TEXT NOT NULL)")
                connection.execute("CREATE INDEX IF NOT EXISTS usage_owner_time ON usage(cost_center_id, stamp, event_id)")
                connection.execute("PRAGMA user_version=1")
                receipts = []
                for event in events:
                    existing = connection.execute("SELECT cost_center_id, stamp, payload FROM usage WHERE event_id=?", (event["event_id"],)).fetchone()
                    if existing:
                        saved = self._decode(existing[2], event["event_id"])
                        if saved["cost_center_id"] != existing[0] or saved["timestamp"] != existing[1]:
                            raise AccountingError("Ledger ownership or timestamp mismatch")
                        if comparable(saved) != comparable(event):
                            raise AccountingConflict(f"Usage identity {event['event_id']} has conflicting facts; no batch changes saved")
                        receipts.append({"status": "duplicate", "event": saved})
                    else:
                        connection.execute("INSERT INTO usage VALUES (?, ?, ?, ?)",
                                           (event["event_id"], event["cost_center_id"], event["timestamp"], json.dumps(event, sort_keys=True)))
                        receipts.append({"status": "recorded", "event": event})
                return receipts
        except sqlite3.Error as error:
            raise AccountingError("Cannot write the accounting ledger; check its directory or database") from error

    @staticmethod
    def _decode(payload, event_id):
        try:
            event = normalize_event(json.loads(payload))
            if event["event_id"] != event_id:
                raise AccountingError("Ledger identity mismatch")
            return event
        except (ValueError, TypeError, RecursionError) as error:
            raise AccountingError("Invalid persisted usage event; restore the accounting ledger") from error

    def _read(self, *, cost_center_id=None, since=None, until=None, limit=None):
        since = timestamp(since) if since is not None else None
        until = timestamp(until) if until is not None else None
        if since is not None and until is not None and since >= until:
            raise AccountingError("since must precede until; windows are half-open [since, until)")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise AccountingError("limit must be between 1 and 10000")
        if not self.path.exists():
            return [], since, until
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise AccountingError("Unsupported accounting ledger schema")
                # Validate indexed fields before filtering; corrupted indexes must not hide charges.
                events = []
                for event_id, owner, stamp, payload in connection.execute("SELECT event_id, cost_center_id, stamp, payload FROM usage ORDER BY stamp, event_id"):
                    event = self._decode(payload, event_id)
                    if event["cost_center_id"] != owner or event["timestamp"] != stamp:
                        raise AccountingError("Ledger ownership or timestamp mismatch")
                    if ((cost_center_id is None or owner == cost_center_id) and
                            (since is None or stamp >= since) and (until is None or stamp < until)):
                        events.append(event)
                return (events[-limit:] if limit is not None else events), since, until
        except sqlite3.Error as error:
            raise AccountingError("Cannot read the accounting ledger; check its directory or database") from error

    def list_events(self, cost_center_id=None, *, since=None, until=None, limit=1000):
        return self._read(cost_center_id=cost_center_id, since=since, until=until, limit=limit)[0]

    def summary(self, cost_center_id=None, *, since=None, until=None, occupied_seconds=None):
        events, since, until = self._read(cost_center_id=cost_center_id, since=since, until=until)
        return summarize(events, cost_center_id=cost_center_id, since=since, until=until, occupied_seconds=occupied_seconds)
