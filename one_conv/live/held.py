"""Messages held for a busy session until it is idle (`live send --when idle`).

A message sent with when=idle to a session in the middle of a turn is not injected at its
next tool boundary: it is stored here, wrapped and under its message id, and this machine's
bridge (`one-conv live serve`) hands it to the session's adapter once the session reports it
is no longer busy. Each held message is delivered at most once: its row is marked
`delivering` before the adapter runs, and a row found in that state after a restart is
reported `unconfirmed` rather than retried. A message whose session stops being listed for
GONE_SECONDS, or that waited MAX_HOLD_SECONDS, is dropped as `expired` with the reason.

A message that came through the relay keeps its relay delivery id; the bridge acks the relay
with the final outcome once the message leaves this store.
"""

import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .model import live_home

logger = logging.getLogger(__name__)

WHEN = ("now", "idle")
# Statuses of a session in the middle of a turn: Claude Code and Codex report "waiting" while
# a turn is blocked on an approval or a question, which is still the same run.
BUSY = frozenset({"busy", "waiting"})
# A session that restarts or resumes is listed again within this; one gone longer has ended.
GONE_SECONDS = 15 * 60
MAX_HOLD_SECONDS = 24 * 3600
# Relay outcomes for each final local status.
RELAY_OUTCOMES = {"delivered": "accepted", "unconfirmed": "unconfirmed", "refused": "refused", "expired": "refused"}


def is_busy(session) -> bool:
    return session.status in BUSY


def held_label(target: str) -> str:
    return f"held until {target} is idle"


def store_path() -> Path:
    return live_home() / "held.db"


def _db() -> sqlite3.Connection:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS held (key TEXT PRIMARY KEY, id TEXT, target TEXT NOT NULL,"
               " sender TEXT NOT NULL, text TEXT NOT NULL, held REAL NOT NULL, seen REAL NOT NULL,"
               " status TEXT NOT NULL, detail TEXT, finished REAL, delivery TEXT, acked INTEGER NOT NULL DEFAULT 0)")
    return db


def bridge_running() -> bool:
    """Whether `live serve` holds its lock on this machine; only it delivers held messages."""
    from .bridge import _held_by_other, bridge_dir
    return _held_by_other(bridge_dir() / "serve.lock")


def hold(message_id: str, target: str, sender: str, text: str, delivery: str | None = None) -> bool:
    """Store one wrapped message for `target`; False when its key is already held (a retry)."""
    now = time.time()
    with closing(_db()) as db, db:
        return bool(db.execute(
            "INSERT OR IGNORE INTO held (key, id, target, sender, text, held, seen, status, detail, delivery)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'held', ?, ?)",
            (delivery or message_id, message_id, target, sender, text, now, now, held_label(target), delivery)).rowcount)


def lookup(message_id: str) -> dict | None:
    """The newest held row sent under this message id, as `live status` shows it."""
    if not store_path().exists():
        return None
    with closing(_db()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM held WHERE id = ? ORDER BY held DESC LIMIT 1", (message_id,)).fetchone()
    return dict(row) if row else None


def by_delivery(delivery: str) -> dict | None:
    if not store_path().exists():
        return None
    with closing(_db()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM held WHERE delivery = ?", (delivery,)).fetchone()
    return dict(row) if row else None


def _finish(db, key: str, status: str, detail: str) -> None:
    db.execute("UPDATE held SET status = ?, detail = ?, finished = ? WHERE key = ?", (status, detail, time.time(), key))


def deliver_due(list_all, adapters, now: float | None = None) -> list[dict]:
    """Hand every held message whose session is now idle to its adapter; returns the rows finished.

    `list_all(harness)` lists live sessions and `adapters[harness].deliver(session, text)` delivers,
    as in the router, so tests can stand in for both.
    """
    if not store_path().exists():
        return []
    now = time.time() if now is None else now
    finished = []
    with closing(_db()) as db:
        db.row_factory = sqlite3.Row
        # A row left `delivering` means the bridge stopped mid-delivery: the session may have it.
        for row in db.execute("SELECT key FROM held WHERE status = 'delivering'").fetchall():
            with db:
                _finish(db, row["key"], "unconfirmed", "the bridge stopped while delivering it")
        rows = [dict(r) for r in db.execute("SELECT * FROM held WHERE status = 'held' ORDER BY held").fetchall()]
        listed: dict[str, list] = {}
        for row in rows:
            harness = row["target"].partition(":")[0]
            if harness not in listed:
                listed[harness] = list_all(harness)
            session = next((s for s in listed[harness] if s.address == row["target"]), None)
            outcome = None
            if now - row["held"] >= MAX_HOLD_SECONDS:
                outcome = ("expired", f"{row['target']} stayed busy for {MAX_HOLD_SECONDS // 3600} hours")
            elif session is None:
                if now - row["seen"] >= GONE_SECONDS:
                    outcome = ("expired", f"{row['target']} ended before it was idle")
            elif is_busy(session):
                with db:
                    db.execute("UPDATE held SET seen = ? WHERE key = ?", (now, row["key"]))
            else:
                with db:
                    db.execute("UPDATE held SET status = 'delivering' WHERE key = ?", (row["key"],))
                outcome = _deliver(adapters, session, row["text"])
            if outcome is None:
                continue
            with db:
                _finish(db, row["key"], *outcome)
            logger.info("held message %s for %s: %s (%s)", row["id"], row["target"], *outcome)
            finished.append({**row, "status": outcome[0], "detail": outcome[1]})
    return finished


def _deliver(adapters, session, text: str) -> tuple[str, str]:
    try:
        detail = adapters[session.harness].deliver(session, text)
    except Exception as error:  # noqa: BLE001 -- any adapter failure may follow a partial send.
        return "unconfirmed", f"{type(error).__name__}: {error}"
    return ("unconfirmed" if str(detail).startswith(("pending", "unconfirmed")) else "delivered"), str(detail)


def unacked() -> list[dict]:
    """Finished relay deliveries whose final outcome the relay has not been told yet."""
    if not store_path().exists():
        return []
    with closing(_db()) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT * FROM held WHERE delivery IS NOT NULL AND acked = 0 AND status NOT IN ('held', 'delivering')")]


def mark_acked(delivery: str) -> None:
    with closing(_db()) as db, db:
        db.execute("UPDATE held SET acked = 1 WHERE delivery = ?", (delivery,))
