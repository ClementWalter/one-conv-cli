"""Off-box copies of the controller's state, and rebuilding a controller from them.

A trusted node pulls two things (`/v1/backup/*`, staffing authority required):

- `state`: a gzip tar of every SQLite database in the controller directory, copied with SQLite's
  online backup so a write in progress never tears it, plus the small JSON files beside them.
  The conversation archive and the local `backups/` folder are left out.
- `archive`: the conversation archive's rows after a cursor. Chunks and revisions are only ever
  inserted, so their rowid is the cursor; a conversation head moves forward through
  `change_sequence`. Pulling heads first, then revisions, then chunks keeps every head the mirror
  holds backed by rows it also holds, since an uploader writes chunks before the head.

`restore` writes the newest state snapshot and the archive mirror into an empty controller
directory.
"""

import base64
import io
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

SQLITE_SUFFIXES = (".sqlite3", ".db")
# The archive has its own incremental pull; `backups/` holds pre-deploy copies of what is sent anyway.
SKIPPED = ("archive", "backups")
# Small enough for one HTTP response the bridge holds in memory.
PAGE_BYTES = 16 * 1024 * 1024
ARCHIVE_DB = Path("archive") / "archive.db"
# Each table's cursor column, in pull order.
ARCHIVE_TABLES = {"conversations_v1": "change_sequence", "revisions_v1": "rowid", "chunks_v1": "rowid"}
BLOB_COLUMNS = {"data"}


def _sqlite_copy(source: Path, target: Path) -> None:
    """A consistent copy of a live database; readers and writers keep going meanwhile."""
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live, sqlite3.connect(target) as copy:
        live.backup(copy)
    copy.close()
    live.close()


def state_snapshot(directory: Path) -> bytes:
    """The controller directory minus the archive, as a gzip tar."""
    directory = Path(directory)
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory() as scratch, tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if relative.parts[0] in SKIPPED or not path.is_file() or path.name.endswith(".lock") or "-journal" in path.name:
                continue
            if path.suffix in SQLITE_SUFFIXES:
                copy = Path(scratch) / relative.name
                _sqlite_copy(path, copy)
                tar.add(copy, arcname=str(relative))
                copy.unlink()
            else:
                tar.add(path, arcname=str(relative))
    return buffer.getvalue()


def archive_page(database: Path, table: str, after: int, max_bytes: int = PAGE_BYTES) -> dict:
    """Rows of one archive table past `after`, in cursor order, about `max_bytes` at most.

    Always returns at least one row when there is one, so a single oversized chunk still moves.
    """
    if table not in ARCHIVE_TABLES:
        raise ValueError(f"table must be one of {', '.join(ARCHIVE_TABLES)}")
    cursor = ARCHIVE_TABLES[table]
    rows, size, last, more = [], 0, after, False
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        schema = [sql for (sql,) in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid")]
        state = dict(db.execute("SELECT sequence, generation FROM archive_state_v1 WHERE id = 1").fetchone())
        for row in db.execute(f"SELECT rowid AS rowid_, * FROM {table} WHERE {cursor} > ? ORDER BY {cursor}", (after,)):
            item = {key: (base64.b64encode(row[key]).decode() if key in BLOB_COLUMNS else row[key]) for key in row.keys()}
            size += sum(len(value) if isinstance(value, str) else 8 for value in item.values())
            if rows and size > max_bytes:
                more = True
                break
            rows.append(item)
            last = row["rowid_" if cursor == "rowid" else cursor]
    db.close()
    return {"table": table, "rows": rows, "next": last, "more": more,
            "schema": schema, "state": state}


def mirror_cursor(database: Path, table: str) -> int:
    """Where the mirror's pull of `table` resumes: its own highest cursor value."""
    if not database.exists():
        return 0
    column = ARCHIVE_TABLES[table]
    with sqlite3.connect(database) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone()
        value = db.execute(f"SELECT max({column}) FROM {table}").fetchone()[0] if exists else None
    db.close()
    return value or 0


def apply_page(database: Path, page: dict) -> int:
    """Write one page into the mirror in a single transaction, keeping the controller's rowids."""
    if not database.exists():
        # The mirror holds every node's conversations: private from its first byte.
        os.close(os.open(database, os.O_WRONLY | os.O_CREAT, 0o600))
    with sqlite3.connect(database) as db:
        for sql in page["schema"]:
            db.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
                          .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
                          .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
        table = page["table"]
        for row in page["rows"]:
            values = {key: (base64.b64decode(value) if key in BLOB_COLUMNS else value) for key, value in row.items()}
            values["rowid"] = values.pop("rowid_")
            if table == "conversations_v1":
                # A moved head keeps its key; its old row goes so the unique sequence index holds.
                db.execute("DELETE FROM conversations_v1 WHERE key = ?", (values["key"],))
            columns = ", ".join(f'"{key}"' for key in values)
            db.execute(f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({', '.join('?' for _ in values)})",
                       tuple(values.values()))
        if table == "conversations_v1":
            # The heads' own snapshot: a later sequence would claim heads the mirror has not pulled.
            db.execute("INSERT OR REPLACE INTO archive_state_v1 (id, sequence, generation) VALUES (1, ?, ?)",
                       (page["state"]["sequence"], page["state"]["generation"]))
    db.close()
    return len(page["rows"])


def mirror_generation(database: Path) -> str | None:
    if not database.exists():
        return None
    with sqlite3.connect(database) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'archive_state_v1'").fetchone()
        row = db.execute("SELECT generation FROM archive_state_v1 WHERE id = 1").fetchone() if exists else None
    db.close()
    return row[0] if row else None


def newest_state(source: Path) -> Path:
    snapshots = sorted((Path(source) / "state").glob("state-*.tar.gz"))
    if not snapshots:
        raise FileNotFoundError(f"no state-*.tar.gz in {Path(source) / 'state'}")
    return snapshots[-1]


def restore(source: Path, directory: Path) -> dict:
    """Rebuild a controller directory from a pulled backup; refuses a directory that already has a hub."""
    source, directory = Path(source), Path(directory)
    if (directory / "hub.sqlite3").exists():
        raise FileExistsError(f"{directory} already holds a hub; restore only into a fresh controller directory")
    snapshot = newest_state(source)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(snapshot, "r:gz") as tar:
        # The data filter refuses absolute paths, links out of the tree and device files.
        tar.extractall(directory, filter="data")
    for path in directory.rglob("*"):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    mirror = source / "archive.db"
    restored_archive = mirror.exists()
    if restored_archive:
        target = directory / ARCHIVE_DB
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sqlite_copy(mirror, target)
        os.chmod(target, 0o600)
        with sqlite3.connect(target) as db:
            # A new generation makes every reader drop its cached listing and reconcile with this copy.
            db.execute("UPDATE archive_state_v1 SET generation = ? WHERE id = 1", (uuid.uuid4().hex,))
        db.close()
    with sqlite3.connect(directory / "hub.sqlite3") as db:
        check = db.execute("PRAGMA quick_check").fetchone()[0]
    db.close()
    if check != "ok":
        raise RuntimeError(f"the restored hub database failed its integrity check: {check}")
    return {"state": snapshot.name, "archive": restored_archive, "directory": str(directory),
            "snapshot_age_hours": round((time.time() - snapshot.stat().st_mtime) / 3600, 1)}


def free_bytes(path: Path) -> int:
    probe = Path(path)
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free
