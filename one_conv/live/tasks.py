"""Requests the human makes, each owned by one agent at a time.

A task is the human's request as one shared piece of work. It is keyed by the post that made
it ("node:message-id"), so a retried post finds the same task. One agent owns it; the others
see the request as context and do not act on it. The owner works it, reports progress, can
hand parts of it to collaborators as child tasks, and is the only one who answers it.

`version` is the ownership epoch: it changes only when the owner changes. Every owner action
carries the version the agent was given, so an agent that lost the task (reassigned while it
was away) is refused instead of acting on it. Nothing reopens or reassigns a task by itself:
an absent owner is shown as such, and only the human reassigns.

The functions take an open SQLite connection inside the caller's transaction (BEGIN
IMMEDIATE), so a task is created in the same commit as the post that made it, and two
concurrent claims cannot both win.
"""

import hashlib
import re
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (key TEXT PRIMARY KEY, post_seq INTEGER, topic TEXT, parent TEXT,
    kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', owner TEXT, state TEXT NOT NULL,
    version INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS tasks_by_topic ON tasks (topic, created);
CREATE INDEX IF NOT EXISTS tasks_by_parent ON tasks (parent, created);
CREATE INDEX IF NOT EXISTS tasks_by_post ON tasks (post_seq);
CREATE TABLE IF NOT EXISTS task_events (key TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, state TEXT NOT NULL, owner TEXT, note TEXT NOT NULL DEFAULT '', at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS task_events_by_key ON task_events (key, at);
CREATE TABLE IF NOT EXISTS channel_leads (topic TEXT PRIMARY KEY, lead TEXT NOT NULL, set_by TEXT NOT NULL,
    at REAL NOT NULL);
"""

STATES = ("unassigned", "assigned", "working", "blocked", "done", "cancelled")
FINISHED = ("done", "cancelled")
# What an owner reports; "assigned" and "unassigned" come only from ownership changes.
REPORTED = ("working", "blocked", "done", "cancelled")
KINDS = ("request", "read", "implementation", "review")
CHILD_KINDS = ("read", "implementation", "review")
NOTE_LIMIT = 2000
TITLE_LIMIT = 200
KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,31}:[0-9a-f]{32}(?:/[a-z]+-[0-9a-f]{8})*")
# A lead is a published name or a full route; the route is resolved when a request arrives.
LEAD = re.compile(r"@[a-z0-9][a-z0-9-]{0,31}|[a-z0-9][a-z0-9-]{0,31}/[a-z]+:[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
DEFAULT_LEAD = "@coordinator"


class TaskError(Exception):
    """A task action the store refuses; `task` is the current state, so a caller never has to guess."""

    def __init__(self, status: int, message: str, task: dict | None = None):
        super().__init__(message)
        self.status, self.task = status, task


def ensure(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def key_for(node: str, message_id: str) -> str:
    return f"{node}:{message_id}"


def check_key(key: object) -> str:
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise TaskError(400, "a task key is NODE:message-id, as `live task list` shows it")
    return key


def _note(note: object) -> str:
    if note is None:
        return ""
    if not isinstance(note, str) or len(note) > NOTE_LIMIT:
        raise TaskError(400, f"a note is text of at most {NOTE_LIMIT} characters")
    return note.strip()


def _row(values) -> dict:
    (key, post_seq, topic, parent, kind, title, owner, state, version, note, created_by,
     created, updated) = values
    return {"key": key, "post": post_seq, "topic": topic, "parent": parent, "kind": kind, "title": title,
            "owner": owner, "state": state, "version": version, "note": note, "created_by": created_by,
            "created": created, "updated": updated}


COLUMNS = ("key, post_seq, topic, parent, kind, title, owner, state, version, note, created_by, created, "
           "updated")


def get(db: sqlite3.Connection, key: str) -> dict | None:
    values = db.execute(f"SELECT {COLUMNS} FROM tasks WHERE key = ?", (key,)).fetchone()
    return _row(values) if values else None


def require(db: sqlite3.Connection, key: str) -> dict:
    task = get(db, check_key(key))
    if task is None:
        raise TaskError(404, f"no task {key}; list open ones with `one-conv live task list`")
    return task


def children(db: sqlite3.Connection, key: str) -> list[dict]:
    return [_row(values) for values in
            db.execute(f"SELECT {COLUMNS} FROM tasks WHERE parent = ? ORDER BY created", (key,))]


def listing(db: sqlite3.Connection, *, topic: str | None = None, owner: str | None = None,
            open_only: bool = False, limit: int = 200) -> list[dict]:
    clauses, values = [], []
    if topic is not None:
        clauses.append("topic = ?")
        values.append(topic)
    if owner is not None:
        clauses.append("owner = ?")
        values.append(owner)
    if open_only:
        clauses.append(f"state NOT IN ({','.join('?' * len(FINISHED))})")
        values.extend(FINISHED)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(f"SELECT {COLUMNS} FROM tasks {where} ORDER BY created DESC LIMIT ?",
                      (*values, max(1, min(limit, 500)))).fetchall()
    return [_row(values) for values in rows]


def for_posts(db: sqlite3.Connection, seqs: list[int]) -> dict[int, dict]:
    """The root task each of these posts created, by post sequence."""
    if not seqs:
        return {}
    rows = db.execute(f"SELECT {COLUMNS} FROM tasks WHERE parent IS NULL AND post_seq IN "
                      f"({','.join('?' * len(seqs))})", seqs).fetchall()
    return {task["post"]: task for task in map(_row, rows)}


def _event(db, task: dict, actor: str, action: str) -> None:
    db.execute("INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (task["key"], task["version"], actor, action, task["state"], task["owner"], task["note"],
                task["updated"]))


def events(db: sqlite3.Connection, key: str) -> list[dict]:
    return [{"version": v, "actor": a, "action": act, "state": s, "owner": o, "note": n, "at": at}
            for v, a, act, s, o, n, at in db.execute(
                "SELECT version, actor, action, state, owner, note, at FROM task_events WHERE key = ? "
                "ORDER BY at, rowid", (key,))]


def _save(db, task: dict) -> dict:
    db.execute("UPDATE tasks SET owner = ?, state = ?, version = ?, note = ?, updated = ? WHERE key = ?",
               (task["owner"], task["state"], task["version"], task["note"], task["updated"], task["key"]))
    return task


def create(db: sqlite3.Connection, key: str, *, post_seq: int | None, topic: str | None, owner: str | None,
           created_by: str, title: str = "", kind: str = "request", parent: str | None = None,
           note: str = "") -> tuple[dict, bool]:
    """The task for this key, created if new; (task, created). A retry finds the first one."""
    existing = get(db, key)
    if existing is not None:
        return existing, False
    if kind not in KINDS:
        raise TaskError(400, f"kind must be one of {', '.join(KINDS)}")
    now = time.time()
    task = {"key": key, "post": post_seq, "topic": topic, "parent": parent, "kind": kind,
            "title": title.strip().splitlines()[0][:TITLE_LIMIT] if title.strip() else "",
            "owner": owner, "state": "assigned" if owner else "unassigned", "version": 1, "note": _note(note),
            "created_by": created_by, "created": now, "updated": now}
    db.execute(f"INSERT INTO tasks ({COLUMNS}) VALUES ({','.join('?' * 13)})",
               (task["key"], post_seq, topic, parent, kind, task["title"], owner, task["state"], 1, task["note"],
                created_by, now, now))
    _event(db, task, created_by, "create")
    return task, True


def _fence(task: dict, actor: str, version: object) -> None:
    """Only the current owner, holding the current version, acts on a task."""
    if task["owner"] != actor:
        holder = task["owner"] or "nobody"
        raise TaskError(409, f"{task['key']} is owned by {holder}, not {actor}; you have no work on it", task)
    if type(version) is not int or version != task["version"]:
        raise TaskError(409, f"{task['key']} is at version {task['version']}, not {version!r}: it changed hands "
                             "since you were given it; check `one-conv live task show` before acting", task)


def claim(db: sqlite3.Connection, key: str, actor: str, version: object = None) -> dict:
    """Take an unowned task, or confirm one you already own; a retry is a no-op.

    A task someone else owns is refused with its current state: claiming never takes work away.
    """
    task = require(db, key)
    if task["owner"] == actor and task["state"] not in FINISHED:
        if version is not None and version != task["version"]:
            _fence(task, actor, version)
        return task
    if task["state"] in FINISHED:
        raise TaskError(409, f"{key} is already {task['state']}", task)
    if task["owner"] is not None:
        raise TaskError(409, f"{key} is owned by {task['owner']}; ask the human to reassign it if needed", task)
    if version is not None and version != task["version"]:
        raise TaskError(409, f"{key} is at version {task['version']}, not {version!r}", task)
    task.update(owner=actor, state="assigned", version=task["version"] + 1, updated=time.time())
    _event(db, _save(db, task), actor, "claim")
    return task


def update(db: sqlite3.Connection, key: str, actor: str, version: object, state: object = None,
           note: object = None) -> dict:
    """The owner reports progress: working, blocked, done or cancelled, with a short note."""
    task = require(db, key)
    _fence(task, actor, version)
    if state is not None and state not in REPORTED:
        raise TaskError(400, f"state must be one of {', '.join(REPORTED)}")
    if task["state"] in FINISHED:
        raise TaskError(409, f"{key} is already {task['state']}", task)
    if state is None and note is None:
        raise TaskError(400, "give a --state, a --note, or both")
    if state == "done" and any(child["state"] not in FINISHED for child in children(db, key)):
        raise TaskError(409, f"{key} still has open child tasks; finish or cancel them first", task)
    if state is not None:
        task["state"] = state
    if note is not None:
        task["note"] = _note(note)
    task["updated"] = time.time()
    _event(db, _save(db, task), actor, "update")
    return task


def child_key(parent: str, kind: str, to: str) -> str:
    return f"{parent}/{kind}-{hashlib.sha256(to.encode()).hexdigest()[:8]}"


def delegate(db: sqlite3.Connection, key: str, actor: str, version: object, to: str, kind: str,
             note: object) -> tuple[dict, bool]:
    """The owner grants part of the work to a collaborator as a child task; (child, created).

    The collaborator works only that child and reports to the owner. The same grant retried
    returns the same child. A finished child of the same kind for the same agent is reopened
    as a new grant only if the owner asks again after it finished.
    """
    task = require(db, key)
    _fence(task, actor, version)
    if kind not in CHILD_KINDS:
        raise TaskError(400, f"a child task kind is one of {', '.join(CHILD_KINDS)}")
    if to == actor:
        raise TaskError(400, "you already own this task; delegate to another agent")
    if task["state"] in FINISHED:
        raise TaskError(409, f"{key} is already {task['state']}", task)
    child = child_key(key, kind, to)
    existing = get(db, child)
    if existing is not None and existing["state"] not in FINISHED:
        return existing, False
    if existing is not None:
        existing.update(owner=to, state="assigned", version=existing["version"] + 1, note=_note(note),
                        updated=time.time())
        _event(db, _save(db, existing), actor, "regrant")
        return existing, True
    return create(db, child, post_seq=task["post"], topic=task["topic"], owner=to, created_by=actor,
                  title=task["title"], kind=kind, parent=key, note=_note(note))


def transfer(db: sqlite3.Connection, key: str, authority: str, to: str | None, note: object = None) -> dict:
    """The human gives the task to another agent, or unassigns it; the old owner's version stops working."""
    task = require(db, key)
    if task["state"] in FINISHED:
        raise TaskError(409, f"{key} is already {task['state']}", task)
    if to == task["owner"]:
        return task
    task.update(owner=to, state="assigned" if to else "unassigned", version=task["version"] + 1,
                updated=time.time())
    if note is not None:
        task["note"] = _note(note)
    _event(db, _save(db, task), authority, "transfer")
    return task


def check_lead(lead: object) -> str:
    if not isinstance(lead, str) or not LEAD.fullmatch(lead):
        raise TaskError(400, "a lead is @name or a full NODE/harness:id route")
    return lead


def set_lead(db: sqlite3.Connection, topic: str, lead: str | None, set_by: str) -> str | None:
    """Who owns new human requests in this channel; None goes back to the default."""
    if lead is None:
        db.execute("DELETE FROM channel_leads WHERE topic = ?", (topic,))
        return None
    db.execute("INSERT OR REPLACE INTO channel_leads VALUES (?, ?, ?, ?)",
               (topic, check_lead(lead), set_by, time.time()))
    return lead


def lead_for(db: sqlite3.Connection, topic: str) -> str:
    row = db.execute("SELECT lead FROM channel_leads WHERE topic = ?", (topic,)).fetchone()
    return row[0] if row else DEFAULT_LEAD
