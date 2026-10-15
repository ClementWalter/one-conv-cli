"""Validate human messages and claim each browser send durably before delivery."""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing

from . import router, team
from .model import live_home, parse_address

logger = logging.getLogger(__name__)
BODY_LIMIT = 65_536
TEXT_LIMIT = 32_000
MESSAGE_ID = re.compile(r"[0-9a-f]{32}")


class SendError(Exception):
    """A browser request that cannot be accepted without changing its input."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def validate(payload: object) -> dict:
    """Accept only the message fields; browser input never chooses the sender."""
    if not isinstance(payload, dict) or set(payload) != {"id", "target", "text"}:
        raise SendError("Provide only id, target and text; the workspace always sends as You.")
    ident, target, text = (payload[key] for key in ("id", "target", "text"))
    if not isinstance(ident, str) or not MESSAGE_ID.fullmatch(ident):
        raise SendError("id must be 32 lowercase hexadecimal characters; keep it unchanged when retrying.")
    if not isinstance(text, str) or not text.strip():
        raise SendError("Write a nonempty message before sending.")
    if len(text) > TEXT_LIMIT:
        raise SendError(f"Messages can contain at most {TEXT_LIMIT:,} characters.", 413)
    if not isinstance(target, str) or len(target) > 512:
        raise SendError("Choose a #topic or a live session address.")
    if target.startswith("#"):
        try:
            team._check("topic", target[1:])
        except team.TeamError:
            raise SendError("Choose a #topic using 1–32 letters, digits or hyphens.") from None
    else:
        _, local = router.remote_address(target)
        if parse_address(local)[0] is None:
            raise SendError("Choose a #topic or a live session address from the workspace.")
    return {"id": ident, "target": target, "text": text}


def _database():
    """Open the durable send journal; a committed claim prevents duplicate prompts."""
    directory = live_home()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "web-messages.sqlite3"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Receipts and failure details remain private even under a permissive umask.
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    db = sqlite3.connect(path, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, digest TEXT NOT NULL,"
               " request TEXT NOT NULL, response TEXT, http_status INTEGER, detail TEXT, claimed REAL NOT NULL)")
    return db


def _unconfirmed(payload: dict) -> dict:
    return {"id": payload["id"], "status": "unconfirmed",
            "error": "Delivery is unconfirmed. Check the conversation before sending another message; "
                     "retrying this message will not send it again.",
            "receipt": {"id": payload["id"], "from": "user", "to": payload["target"], "status": "unconfirmed"}}


def _claim(payload: dict) -> tuple[bool, int, dict]:
    request = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(request.encode()).hexdigest()
    with closing(_database()) as db, db:
        inserted = db.execute("INSERT OR IGNORE INTO messages (id, digest, request, claimed) VALUES (?, ?, ?, ?)",
                              (payload["id"], digest, request, time.time())).rowcount
        if inserted:
            return True, 200, _unconfirmed(payload)
        stored_digest, response, status = db.execute(
            "SELECT digest, response, http_status FROM messages WHERE id = ?", (payload["id"],)).fetchone()
    if stored_digest != digest:
        raise SendError("This message id already belongs to different content. Start a new message.", 409)
    return False, status or 200, json.loads(response) if response else _unconfirmed(payload)


def _finish(ident: str, status: int, response: dict, detail: str | None = None) -> None:
    with closing(_database()) as db, db:
        db.execute("UPDATE messages SET response = ?, http_status = ?, detail = ? WHERE id = ?",
                   (json.dumps(response), status, detail, ident))


def _accepted(receipt: dict) -> bool:
    statuses = [str(row.get("status", "")) for row in receipt.get("deliveries", [])]
    if "deliveries" not in receipt:
        statuses.append(str(receipt.get("status", "")))
    # A queued harness inbox is accepted; an unknown adapter or hub handoff is not.
    unknown = ("pending", "unconfirmed", "refused", "not delivered", "queued for the hub")
    return all(status and not status.lower().startswith(unknown) for status in statuses)


def send(payload: dict) -> tuple[int, dict]:
    """Return a receipt once, replaying the durable result for every identical retry."""
    payload = validate(payload)
    claimed, status, response = _claim(payload)
    if not claimed:
        return status, response
    detail = None
    try:
        receipt = router.send(payload["target"], payload["text"], sender=None, origin="user",
                              message_id=payload["id"])
    except Exception as error:
        # An adapter can raise after accepting a prompt, so its claim must never be retried.
        logger.exception("Human message %s has an unconfirmed outcome", payload["id"])
        detail = f"{type(error).__name__}: {error}"
        response = _unconfirmed(payload)
    else:
        response = {"id": payload["id"], "status": "sent" if _accepted(receipt) else "unconfirmed",
                    "receipt": receipt}
    try:
        _finish(payload["id"], status, response, detail)
    except (OSError, sqlite3.Error):
        # The initial claim survives a result-write failure and still prevents another prompt.
        logger.exception("Could not save the result for human message %s", payload["id"])
        return 200, _unconfirmed(payload)
    return status, response
