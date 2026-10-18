"""Read marks: how far the human has read each conversation, shared by every device of one workspace.

A conversation is `#topic` or a direct pair ("a b", the two addresses sorted). Its mark is the send time
of the newest message the human has seen there. A mark only moves forward, so a device showing an older
copy never makes newer messages unread again. A conversation without a mark counts from the moment read
tracking started, so history from before that starts read.
"""

import math
import re
import sqlite3
import time

from .hub import TOPIC

# The row holding the moment read tracking started; it can never collide with a conversation key.
BASELINE = "*"
CONVERSATION_LIMIT = 1024
HUMAN = re.compile(r"(?:[a-z0-9][a-z0-9-]{0,31}/)?user")


class ReadError(ValueError):
    pass


def validate(payload: object) -> dict:
    """The browser names one conversation and the send time of the newest message it showed."""
    if not isinstance(payload, dict) or set(payload) != {"conversation", "at"}:
        raise ReadError("Provide only conversation and at.")
    conversation, at = payload["conversation"], payload["at"]
    if not isinstance(conversation, str) or not conversation or len(conversation) > CONVERSATION_LIMIT:
        raise ReadError("conversation is a #topic or a direct pair of two addresses.")
    if conversation.startswith("#"):
        if not TOPIC.fullmatch(conversation[1:]):
            raise ReadError("Choose a valid #topic.")
    else:
        pair = conversation.split(" ")
        if len(pair) != 2 or not all(pair) or pair != sorted(pair) or re.search(r"[\s#]", "".join(pair)):
            raise ReadError("A direct pair is two sorted addresses separated by one space.")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
        raise ReadError("at is the send time, in seconds, of the newest message shown.")
    # A mark in the future would hide messages that have not been sent yet.
    return {"conversation": conversation, "at": min(float(at), time.time())}


def is_human(address) -> bool:
    return bool(HUMAN.fullmatch(str(address)))


def count(messages: list[dict], mark: float) -> int:
    """Messages after the mark that someone other than the human sent."""
    return sum(1 for message in messages
               if not is_human(message.get("from")) and _time(message.get("at")) > mark)


def _time(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def ensure(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS read_marks (conversation TEXT PRIMARY KEY, at REAL NOT NULL)")
    db.execute("INSERT OR IGNORE INTO read_marks VALUES (?, ?)", (BASELINE, time.time()))


def marks(db: sqlite3.Connection) -> tuple[float, dict[str, float]]:
    """(baseline, {conversation: mark})."""
    rows = dict(db.execute("SELECT conversation, at FROM read_marks"))
    return rows.pop(BASELINE, 0.0), rows


def mark(db: sqlite3.Connection, payload: dict) -> float:
    """Move one conversation's mark forward and return the mark now stored."""
    db.execute("INSERT INTO read_marks VALUES (?, ?) ON CONFLICT(conversation) "
               "DO UPDATE SET at = MAX(at, excluded.at)", (payload["conversation"], payload["at"]))
    return db.execute("SELECT at FROM read_marks WHERE conversation = ?", (payload["conversation"],)).fetchone()[0]
