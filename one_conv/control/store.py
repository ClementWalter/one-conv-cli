"""Persist the personal workspace independently of any operator laptop or relay."""

import os
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from ..live.hub import MESSAGE_ID, TOPIC, Hub, HubError, split_address

NODE = "workspace"
HUMAN = "workspace/user"
TEXT_LIMIT = 32_000
MESSAGES_SHOWN = 300


@contextmanager
def private_database(path: Path):
    """Keep browser credentials private even under a permissive process umask."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(path, timeout=10)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def display_address(address: str) -> str:
    return "user" if address == HUMAN else address


def validate_message(payload: object) -> dict:
    """The browser supplies content and a stable retry ID, never a sender identity."""
    if not isinstance(payload, dict) or set(payload) != {"id", "target", "text"}:
        raise HubError(400, "Provide only id, target and text; the sender is always You.")
    ident, target, text = (payload[key] for key in ("id", "target", "text"))
    if not isinstance(ident, str) or not MESSAGE_ID.fullmatch(ident):
        raise HubError(400, "id must be 32 lowercase hexadecimal characters, unchanged on retry")
    if not isinstance(text, str) or not text.strip():
        raise HubError(400, "Write a nonempty message.")
    if len(text) > TEXT_LIMIT:
        raise HubError(413, "Message text is too large.")
    if not isinstance(target, str) or not target or len(target) > 512:
        raise HubError(400, "Choose a published agent route or #topic.")
    try:
        text.encode("utf-8")
        target.encode("utf-8")
    except UnicodeError:
        raise HubError(400, "Message text and target must contain valid Unicode.") from None
    if target.startswith("#"):
        if not TOPIC.fullmatch(target[1:]):
            raise HubError(400, "Choose a valid #topic.")
    else:
        split_address(target)
        if re.search(r"\s", target):
            raise HubError(400, "An agent route cannot contain whitespace.")
    return {"id": ident, "target": target, "text": text}


class ControlStore:
    """The hub posts are the durable mailbox and the idempotent send journal."""

    def __init__(self, directory: Path, hub: Hub | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hub is None:
            path = self.directory / "hub.sqlite3"
            with private_database(path):
                pass
            hub = Hub(str(path))
        else:
            path = Path(hub.path).resolve()
            if not path.is_relative_to(self.directory.resolve()):
                raise ValueError("The cloud hub database must be inside store_dir")
            with private_database(path):
                pass
        self.hub = hub
        self.refresh_human()

    def refresh_human(self):
        """A cloud human has a published return route without a local bridge."""
        self.hub.publish(NODE, [{"address": "user", "name": None, "title": "You", "status": "human"}])
        self.drain_human()

    def drain_human(self):
        """Accepted means durably in this mailbox, never that a person has read it."""
        while True:
            items = self.hub.inbox(NODE, 0)
            for item in items:
                if item["to"] == "user":
                    self.hub.ack(NODE, item["delivery"], "accepted")
                else:
                    self.hub.ack(NODE, item["delivery"], "refused")
            if len(items) < 50:
                return

    def state(self):
        self.refresh_human()
        directory = self.hub.directory()
        return {"at": time.time(), "sessions": [
            {**row, "harness": row["local"].partition(":")[0],
             "id": row["local"].partition(":")[2], "delivery": "hub"}
            for row in directory if row["address"] != HUMAN],
            "names": {row["name"]: display_address(row["address"]) for row in directory if row["name"]},
            "topics": [{**topic, "members": [display_address(member) for member in topic["members"]]}
                       for topic in self.hub.topics()],
            "bridge": {"node": NODE, "running": True, "mode": "cloud"}, "error": None}

    @staticmethod
    def _row(seq, ident, node, sender, target, topic, reply, mode, text, created, outcome):
        return {"cursor": str(seq), "id": ident, "from": display_address(f"{node}/{sender}"),
                "to": display_address(target), "topic": topic, "reply": reply, "mode": mode,
                "text": text, "at": created, "status": outcome or "stored"}

    def direct(self):
        self.drain_human()
        with self.hub._db() as db:
            rows = db.execute(
                "SELECT p.seq,p.id,p.from_node,p.sender,d.to_node || '/' || d.to_address,p.topic,p.reply,p.mode,"
                "p.text,p.created,d.outcome FROM posts p JOIN deliveries d ON d.post=p.seq "
                "WHERE p.topic IS NULL ORDER BY p.seq DESC LIMIT ?", (MESSAGES_SHOWN,)).fetchall()
        threads = defaultdict(list)
        for values in reversed(rows):
            row = self._row(*values)
            pair = " ".join(sorted((row["from"], row["to"])))
            threads[pair].append(row)
        return {"threads": [{"pair": pair, "messages": rows, "last": rows[-1]["at"]}
                            for pair, rows in sorted(threads.items(), key=lambda item: -item[1][-1]["at"])]}

    def topic(self, topic: str):
        if not TOPIC.fullmatch(topic):
            raise HubError(400, "Choose a valid topic.")
        with self.hub._db() as db:
            total = db.execute("SELECT COUNT(*) FROM posts WHERE topic=?", (topic,)).fetchone()[0]
            rows = db.execute(
                "SELECT seq,id,from_node,sender,target,topic,reply,mode,text,created,NULL "
                "FROM posts WHERE topic=? ORDER BY seq DESC LIMIT ?", (topic, MESSAGES_SHOWN)).fetchall()
        return {"topic": topic, "messages": [self._row(*row) for row in reversed(rows)],
                "omitted": max(0, total - len(rows))}

    def send(self, payload):
        """Retrying after any crash reuses the hub's atomic (node, ID, payload) claim."""
        payload = validate_message(payload)
        receipt = self.hub.send(NODE, {"id": payload["id"], "from": "user", "to": payload["target"],
                                       "text": payload["text"], "reply": "expected", "mode": "push"},
                                join_sender=payload["target"].startswith("#"))
        recipients = receipt["recipients"]
        unconfirmed = any(row["status"] in ("refused", "unconfirmed") for row in recipients)
        return {"id": payload["id"], "status": "unconfirmed" if unconfirmed else "sent", "receipt": {
            "id": payload["id"], "from": "user", "to": payload["target"], "via": "hub", "delivery": "hub",
            "status": "delivery unconfirmed" if unconfirmed else "stored on the hub", "duplicate": receipt["duplicate"],
            "deliveries": [{"to": display_address(row["to"]), "status": row["status"], "delivery": row["delivery"]}
                           for row in recipients],
            **({"topic": payload["target"][1:]} if payload["target"].startswith("#") else {}),
            "notice": "Stored durably; delivery acceptance does not mean the recipient has read it."}}
