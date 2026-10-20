"""One-use browser pairing and revocable persistent sessions store only token hashes."""

import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path

from .store import private_database

COOKIE = "__Host-oneconv-session"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL, consumed REAL);
CREATE TABLE IF NOT EXISTS browser_sessions (hash TEXT PRIMARY KEY, expires REAL NOT NULL, revoked REAL);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthStore:
    """Operator pairing links expire; browser sessions survive application restarts."""

    def __init__(self, directory: Path, *, session_seconds: int = 30 * 86400, clock=time.time):
        if isinstance(session_seconds, bool) or not isinstance(session_seconds, int) or not 1 <= session_seconds <= 30 * 86400:
            raise ValueError("session_seconds must be 1-2592000")
        self.path = Path(directory) / "auth.sqlite3"
        self.session_seconds, self.clock = session_seconds, clock
        with private_database(self.path) as db:
            db.executescript(SCHEMA)

    def issue_pairing_token(self, ttl_seconds: int = 600) -> str:
        """Issue once to the operator; a new link supersedes unused earlier links."""
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be 1-3600")
        token = secrets.token_urlsafe(32)
        with private_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM pairing")
            db.execute("INSERT INTO pairing VALUES (?,?,NULL)", (_hash(token), self.clock() + ttl_seconds))
        return token

    def pair(self, token: object, previous: str | None = None) -> str | None:
        """Consume the link and create the session in one transaction."""
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        cookie, now = secrets.token_urlsafe(32), self.clock()
        with private_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE pairing SET consumed=? WHERE hash=? AND consumed IS NULL AND expires>?",
                                 (now, _hash(token), now)).rowcount
            if not changed:
                return None
            db.execute("INSERT INTO browser_sessions VALUES (?,?,NULL)",
                       (_hash(cookie), now + self.session_seconds))
            if previous and TOKEN.fullmatch(previous):
                db.execute("UPDATE browser_sessions SET revoked=? WHERE hash=?", (now, _hash(previous)))
        return cookie

    def session(self, cookie: object) -> dict | None:
        if not isinstance(cookie, str) or not TOKEN.fullmatch(cookie):
            return None
        with private_database(self.path) as db:
            row = db.execute("SELECT expires FROM browser_sessions WHERE hash=? AND revoked IS NULL AND expires>?",
                             (_hash(cookie), self.clock())).fetchone()
        if row is None:
            return None
        return {"expires_at": row[0], "csrf_token": _hash("csrf:" + cookie)}

    def check_csrf(self, cookie: str, supplied: object) -> bool:
        session = self.session(cookie)
        return bool(session and isinstance(supplied, str) and hmac.compare_digest(
            session["csrf_token"].encode(), supplied.encode()))

    def revoke(self, cookie: str) -> bool:
        if not isinstance(cookie, str) or not TOKEN.fullmatch(cookie):
            return False
        with private_database(self.path) as db:
            return bool(db.execute("UPDATE browser_sessions SET revoked=? WHERE hash=? AND revoked IS NULL",
                                   (self.clock(), _hash(cookie))).rowcount)

    def revoke_others(self, cookie: str) -> int:
        """Disconnect every browser but this one and cancel unused pairing links; how many were disconnected."""
        if not isinstance(cookie, str) or not TOKEN.fullmatch(cookie):
            return 0
        with private_database(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM pairing WHERE consumed IS NULL")
            return db.execute("UPDATE browser_sessions SET revoked=? WHERE revoked IS NULL AND hash != ?",
                              (self.clock(), _hash(cookie))).rowcount

    def revoke_all_sessions(self) -> int:
        with private_database(self.path) as db:
            return db.execute("UPDATE browser_sessions SET revoked=? WHERE revoked IS NULL", (self.clock(),)).rowcount
