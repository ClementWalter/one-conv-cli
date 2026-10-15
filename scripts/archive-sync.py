#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click==8.3.3"]
# ///
"""Copy native conversations into an archive. Native files change only through --free-older-than and --restore."""

import argparse
import base64
import dataclasses
import errno
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from one_conv.archive import (
    SYNC_LOCK,
    SYNC_STALE_SECONDS,
    SYNC_STATE,
    ArchiveError,
    configured_client,
    conversation_key,
    digest,
    encode,
    pack,
    read_json,
    revision,
    split,
    write_json,
)

# Captures re-run when the reader or this capture code changes, even if sources did not.
CAPTURE_VERSION = digest((ROOT / "bin/one-conv").read_bytes() + Path(__file__).read_bytes())


def load_reader():
    loader = importlib.machinery.SourceFileLoader("archive_reader", str(ROOT / "bin/one-conv"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    reader = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reader
    loader.exec_module(reader)
    return reader


def origin_id(reader):
    path = reader.STATE_DIR / "archive-origin"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with path.open("x") as stream:
            stream.write(str(uuid.uuid4()))
        path.chmod(0o600)
    except FileExistsError:
        pass
    return path.read_text().strip()


def source_paths(reader, handle):
    if handle.source == "cursor":
        return []  # Its rows live in Cursor's shared database; capture compares them instead.
    paths = [handle.path] if handle.path else []
    if handle.source == "claude":
        # The session folder holds subagent transcripts, full tool outputs, workflows and the title.
        paths.extend(session_files(session_folder(handle)))
    if handle.source == "cursor-cli":
        paths.append(handle.path.parent / "meta.json")
    return paths


def session_folder(handle):
    """A Claude session's folder, or None when it is missing or is itself a symlink."""
    folder = handle.path.parent / handle.uuid
    if folder.is_symlink() or not folder.is_dir():
        return None
    return folder


def session_files(folder):
    """Regular files under a session folder; symlinks are skipped so a capture never reads outside it."""
    if folder is None:
        return []
    files = []
    for directory, _, names in os.walk(folder, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                files.append(path)
    return sorted(files)


def read_inside(root, path):
    """Read `path` through directory descriptors that refuse symlinks at every step.

    Checking for symlinks before reading is not enough: a file or folder can be swapped
    for one between the check and the read, so the refusal happens at the open itself.
    """
    parts = path.relative_to(root).parts
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with os.fdopen(handle, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ArchiveError(f"Not a regular file in a session folder: {path}")
        return stream.read()


def read_source(handle, path):
    """Bytes of one source file, with files of a Claude session folder read through read_inside.

    The folder is its expected path, whatever is there now: a folder replaced by a symlink
    after enumeration must make the read refuse, not route its files to an unguarded read.
    """
    if handle.source == "claude":
        folder = handle.path.parent / handle.uuid
        if folder in path.parents:
            return read_inside(folder, path)
    return read_regular(path)


def read_regular(path):
    """Bytes of `path` only if it is a regular file itself, not a symlink to one."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        # A pathname check before reading would leave a check-then-use race; refuse instead.
        raise ArchiveError("archiving needs no-follow file opens, which this system lacks")
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except OSError as error:
        if error.errno == errno.ELOOP:  # What a no-follow open of a symlink reports.
            raise ArchiveError(f"{path} is a symlink or not a regular file; it is not archived") from error
        raise
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ArchiveError(f"Not a regular file: {path}")
        return stream.read()


def main_files(handle, paths):
    """A conversation's own files outside a Claude session folder: its transcript, a Cursor CLI
    store and that store's meta.json."""
    folder = handle.path.parent / handle.uuid if handle.source == "claude" and handle.path else None
    return [path for path in paths if folder is None or folder not in path.parents]


def require_regular(paths):
    """Refuse files that are symlinks or not regular files. The reader opens a transcript by path,
    so a symlink there would put text from outside history into the archived turns."""
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue  # A missing file fails the capture where it is read.
        if not stat.S_ISREG(mode):
            raise ArchiveError(f"{path} is a symlink or not a regular file; it is not archived")


def source_stamp(reader, handle, paths):
    """One comparable value that changes whenever the conversation's sources do.

    File sources use size and time of every file, write-ahead logs included. Cursor IDE
    conversations are rows in a shared database whose file changes for every chat, so
    their stamp is a hash of the conversation's own rows.
    """
    if handle.source == "cursor":
        rows = original_records(reader, handle, paths)
        return digest(b"".join(rows[name] for name in sorted(rows)))
    return digest(encode(fingerprint(paths)))


def load_stamps(reader):
    try:
        stamps = json.loads((reader.STATE_DIR / "archive-stamps.json").read_text())
    except (OSError, ValueError):
        return {}
    return stamps if isinstance(stamps, dict) else {}


def save_stamps(reader, stamps):
    path = reader.STATE_DIR / "archive-stamps.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(stamps, sort_keys=True))
    os.replace(partial, path)


def fingerprint(paths):
    files = [p for path in paths for p in (path, Path(str(path) + "-wal"))]
    # Reading a checkpointed SQLite store creates an empty -wal; it holds no data, so it counts as absent.
    return [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files
            if p.exists() and not (p.name.endswith("-wal") and p.stat().st_size == 0)]


def snapshot_store(path):
    """A consistent SQLite image of a Cursor CLI store, taken from no-follow copies of the store and
    its write-ahead log in a private folder, so SQLite itself never opens a path that could be a link."""
    with tempfile.TemporaryDirectory(prefix="one-conv-store-") as private:
        copy = Path(private) / path.name
        copy.write_bytes(read_regular(path))
        try:
            Path(str(copy) + "-wal").write_bytes(read_regular(Path(str(path) + "-wal")))
        except FileNotFoundError:
            pass  # A checkpointed store has no write-ahead log.
        with sqlite3.connect(copy) as source, sqlite3.connect(":memory:") as snapshot:
            source.backup(snapshot)
            return snapshot.serialize()


# Sources whose conversation is parsed again from the captured bytes; the rest read cloud caches one-conv wrote itself.
PROTECTED_SOURCES = ("claude", "codex", "omp", "cursor-cli")


def captured_identity(reader, handle, copy):
    """(session id, cwd, title) as discovery would read them, but from the private copy.

    None for a field the copy does not determine: a Claude Code project's cwd comes from its
    folder name, and Claude Code and Codex titles come from the turns, already parsed from the copy.
    """
    if handle.source == "cursor-cli":
        try:
            meta = json.loads((copy.parent / "meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        return handle.uuid, meta.get("cwd"), meta.get("title") or None
    if handle.source == "omp":
        meta = reader._omp_session_meta(copy)
        return meta.get("session_id") or copy.stem, meta.get("cwd"), meta.get("title") or None
    if handle.source == "codex":
        meta = reader._codex_session_meta(copy)
        return meta.get("session_id") or copy.stem, meta.get("cwd"), None
    return handle.uuid, None, None


def rebuilt_handle(reader, handle, copy):
    """`handle` pointed at the copy, with everything discovery read from the live files read again
    from the copy. A session id or cwd that no longer matches the listing is refused, not relabelled."""
    session, cwd, title = captured_identity(reader, handle, copy)
    if session != handle.uuid or (cwd is not None and cwd != handle.project_cwd):
        raise ArchiveError(f"{handle.uuid}: its metadata changed between listing and capture; retry")
    return dataclasses.replace(handle, path=copy, title_hint=title)


@contextmanager
def captured_copy(reader, handle, paths, originals):
    """`handle` pointed at a private copy of its captured bytes, laid out as on disk.

    The reader opens files by path; parsing the copy means turns, summary and title come from the
    same bytes as the archived originals, whatever replaces the real paths meanwhile. What discovery
    read from the live files (session id, cwd, title) is read again from the copy too.
    """
    if handle.source not in PROTECTED_SOURCES or handle.path is None:
        yield handle
        return
    base = handle.path.parent
    with tempfile.TemporaryDirectory(prefix="one-conv-capture-") as private:
        root = Path(private)
        for path in paths:
            target = root / path.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(originals[str(path)])
        yield rebuilt_handle(reader, handle, root / handle.path.relative_to(base))


def original_records(reader, handle, paths):
    if handle.source == "cursor":
        conn = reader._cursor_connect()
        if conn is None:
            raise ArchiveError("Cursor database is unavailable")
        try:
            rows = conn.execute("SELECT key, value FROM cursorDiskKV WHERE key = ? OR key LIKE ?",
                (f"composerData:{handle.composer_id}", f"bubbleId:{handle.composer_id}:%")).fetchall()
            if not rows:
                raise ArchiveError("Cursor conversation records are unavailable")
            header = conn.execute("SELECT composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, value "
                                  "FROM composerHeaders WHERE composerId = ?", (handle.composer_id,)).fetchone()
            # One record keeps a conversation's many small rows in a few chunks.
            return {"cursorDiskKV": encode({key: base64.b64encode(value if isinstance(value, bytes) else value.encode()).decode()
                                            for key, value in sorted(rows, key=lambda row: row[0])}),
                    "composerHeaders": encode(list(header) if header else None)}
        finally:
            conn.close()
    result = {}
    for path in paths:
        # Only the Cursor CLI store is a live database to snapshot; any other file, whatever its
        # name, goes through read_source so a session-folder sidecar stays inside its boundary.
        if handle.source == "cursor-cli" and path == handle.path:
            data = snapshot_store(path)
        else:
            data = read_source(handle, path)
        result[str(path)] = data
    return result


def capture(reader, handle, origin):
    paths = source_paths(reader, handle)
    # Before the reader opens anything by path, and again after, to catch a swap during capture.
    require_regular(main_files(handle, paths))
    before = fingerprint(paths)
    originals = original_records(reader, handle, paths)
    account_label = None
    if handle.source in reader.CLOUD_SOURCES or (handle.path and handle.path.suffix == ".json"):
        cached = json.loads(originals[str(handle.path)]) if handle.path else {}
        if cached.get("complete") is False:
            raise ArchiveError(f"Incomplete provider capture: {handle.uuid}; pull complete history first")
        account_label = cached.get("account_label")
    with captured_copy(reader, handle, paths, originals) as parsed:
        turns = reader._handle_iter_turns(parsed, include_subagents=False)
        expanded = reader._handle_iter_turns(parsed, include_subagents=True) if handle.source == "claude" else None
        summary = reader._handle_summary(parsed, {})
    summary.pop("unread", None)
    document = {"origin": origin, "source": handle.source, "cwd": handle.project_cwd,
        "session": handle.uuid, "locator": reader._record_locator(handle), "mtime": handle.mtime, "capture_version": CAPTURE_VERSION,
        "account_label": account_label, "summary": summary,
        "unread_native": handle.unread_native, "turns": [asdict(turn) for turn in turns],
        "turns_with_subagents": [asdict(turn) for turn in expanded] if expanded != turns and expanded is not None else None,
        "search_text": [text for turn in turns if (text := reader._render_turn(turn, raw=False))],
        "originals": originals}
    changed = original_records(reader, handle, paths) != originals if handle.source == "cursor" else fingerprint(paths) != before
    if changed:
        raise ArchiveError(f"Source changed during capture: {handle.uuid}; retry when idle")
    require_regular(main_files(handle, paths))
    return document


FREEABLE_SOURCES = ("claude", "codex", "omp")  # One file or folder per conversation; never a shared database.
# What one conversation can raise without stopping the others: unreadable, malformed or archive-side errors.
SYNC_ERRORS = (ArchiveError, OSError, ValueError, LookupError, TypeError, AttributeError, sqlite3.Error)


def history_roots(reader, source):
    return {"claude": [reader.PROJECTS_DIR], "codex": reader.CODEX_SESSION_DIRS, "omp": [reader.OMP_SESSIONS_DIR]}[source]


def acquire_lock(path):
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime < SYNC_STALE_SECONDS:
                    return False
                path.unlink()  # The previous sync stopped without releasing it.
            except FileNotFoundError:
                pass
    return False


def running_reason(handle, paths):
    """Why a conversation is still in use, or None. A running agent rewrites or appends to its
    transcript, so freeing it would lose turns and break the agent's own session."""
    from one_conv.live import router
    for session in router.list_all(handle.source):
        if handle.uuid == session.id or handle.uuid.endswith(session.id):
            return f"its session is running ({session.address})"
    lsof = shutil.which("lsof")
    if lsof:
        # An idle agent can hold its transcript open without registering as live.
        holders = subprocess.run([lsof, "-t", "--", *map(str, paths)], capture_output=True, text=True, check=False)
        if holders.stdout.strip():
            return f"a process has its files open (pid {holders.stdout.split()[0]})"
    return None


class PinnedFiles:
    """One conversation's files, reached through directory descriptors opened once without
    following symlinks. Verifying, measuring, deleting and tidying all act on those same
    directories, so a folder replaced by a symlink in between cannot redirect any of them."""

    @staticmethod
    def supported():
        """Whether this system can open folders without following symlinks and act relative to them.

        Checked when freeing, not at import: syncing and help never need these capabilities.
        """
        return (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
                and all(call in os.supports_dir_fd for call in (os.open, os.stat, os.unlink, os.rmdir)))

    def __init__(self, base, paths):
        self.base, self.descriptors = base, {}
        if not self.supported():
            # Without no-follow folder operations a swapped folder could redirect a deletion; never fall back.
            raise ArchiveError("freeing needs no-follow folder operations, which this system lacks")
        self.directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            self.descriptors[()] = os.open(base, self.directory)
            for path in paths:
                self._directory(path.relative_to(base).parts[:-1])
        except (OSError, ValueError) as error:
            self.close()
            raise ArchiveError(f"its folders cannot be opened safely: {error}") from error

    def _directory(self, parts):
        if parts not in self.descriptors:
            parent = self._directory(parts[:-1])
            self.descriptors[parts] = os.open(parts[-1], self.directory, dir_fd=parent)
        return self.descriptors[parts]

    def _locate(self, path):
        parts = path.relative_to(self.base).parts
        return self._directory(parts[:-1]), parts[-1]

    def read(self, path):
        directory, name = self._locate(path)
        with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ArchiveError(f"Not a regular file: {path}")
            return stream.read()

    def size(self, path):
        directory, name = self._locate(path)
        return os.stat(name, dir_fd=directory, follow_symlinks=False).st_size

    def unlink(self, path):
        directory, name = self._locate(path)
        os.unlink(name, dir_fd=directory)  # Removes the entry itself; a symlink found here is not followed.

    def remove_empty_directories(self):
        """Remove pinned subfolders left empty, deepest first, each through its pinned parent."""
        for parts in sorted((p for p in self.descriptors if p), key=len, reverse=True):
            try:
                os.rmdir(parts[-1], dir_fd=self.descriptors[parts[:-1]])
            except OSError:
                pass  # Not empty, or no longer a directory at that name (a symlink is refused, not followed).

    def close(self):
        for descriptor in self.descriptors.values():
            os.close(descriptor)
        self.descriptors = {}


def free(reader, client, handle, archived):
    """Delete a conversation's native files only if the archive holds exactly these bytes and no
    agent is using them; return the bytes freed."""
    paths = source_paths(reader, handle)
    reason = running_reason(handle, paths)
    if reason:
        raise ArchiveError(reason)
    pinned = PinnedFiles(handle.path.parent, paths)
    try:
        before = fingerprint(paths)
        manifest = client.read_conversation(archived["key"], archived["revision"])
        originals = manifest["originals"]
        parts = {path: [name for name, _ in split(pinned.read(path))] for path in paths}
        if any(originals.get(str(path)) != names for path, names in parts.items()):
            raise ArchiveError("its files differ from the archived capture")
        if client.request("chunks/missing", {"digests": [name for names in parts.values() for name in names]})["missing"]:
            raise ArchiveError("the archive is missing some of its content")
        if source_paths(reader, handle) != paths or fingerprint(paths) != before:
            raise ArchiveError("it changed while being verified")
        # Marked in the archive first, so sync refuses a partial file that reappears here, even after a state reset.
        client.put({**manifest, "freed": time.time()}, {}, archived["revision"])
        size = sum(pinned.size(path) for path in paths)
        for path in paths:
            pinned.unlink(path)
        pinned.remove_empty_directories()
        return size
    finally:
        pinned.close()


def restore_target(roots, name):
    """The local path of an archived original, refused unless it lies inside this machine's native history."""
    path = Path(name)
    root = next((root for root in roots if path.is_absolute() and ".." not in path.parts and path.is_relative_to(root)
                 and path != root), None)
    if root is None:
        raise ArchiveError(f"{name} is outside this machine's history folders; nothing was restored")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ArchiveError(f"{current} is a symbolic link; nothing was restored")
    return path


def restore(reader, client, session):
    """Write back each record of a conversation that was freed or is missing here, from its newest copy this
    machine can hold; every target is checked before anything is written."""
    rows = [row for row in client.list_conversations() if row["source"] in FREEABLE_SOURCES and row["session"].startswith(session)]
    # A full ID can prefix another, e.g. Claude Code's `<id>.orphaned-<time>-<hash>` transcripts.
    rows = [row for row in rows if row["session"] == session] or rows
    matches = {(row["source"], row["session"]) for row in rows}
    if len(matches) != 1:
        raise ArchiveError(f"{len(matches)} archived conversations match {session!r}; give the full session ID")
    records, refused = {}, None
    for row in sorted(rows, key=lambda row: row["mtime"], reverse=True):
        manifest = client.read_conversation(row["key"], row["revision"])
        try:  # Another machine's copy may name paths that do not exist here.
            targets = {restore_target(history_roots(reader, row["source"]), name): parts
                       for name, parts in manifest["originals"].items()}
        except ArchiveError as error:
            refused = error
            continue
        records.setdefault(row["locator"], []).append((row, manifest, targets))
    plans = [copies for copies in records.values()
             if any(row.get("freed") for row, _, _ in copies) or not all(path.exists() for path in copies[0][2])]
    if not plans and refused:
        raise refused
    contents = [(copies[0][1], {path: client.read_chunks(parts) for path, parts in copies[0][2].items()}) for copies in plans]
    for _, files in contents:
        for path, data in files.items():
            if path.exists() and path.read_bytes() != data:
                raise ArchiveError(f"{path} exists with different content; move it away to restore")
    restored = 0
    for manifest, files in contents:
        for path, data in files.items():
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(data)
                restored += 1
        main_file = next(iter(files))  # Captures list the conversation's main file first.
        os.utime(main_file, (manifest["mtime"], manifest["mtime"]))  # Native and archived copies compare equal.
    for copies in plans:
        for row, manifest, _ in copies:
            if row.get("freed"):
                client.put({name: value for name, value in manifest.items() if name != "freed"}, {}, row["revision"])
    return {"restored": restored}


def main():
    reader = load_reader()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=reader.SOURCES, action="append")
    parser.add_argument("--project", help="Restrict to project paths containing this text")
    parser.add_argument("--limit", type=int, default=0, help="Maximum conversations (0 = all)")
    parser.add_argument("--free-older-than", type=int, metavar="DAYS",
                        help=f"After syncing, delete native {'/'.join(FREEABLE_SOURCES)} files unchanged for DAYS "
                             "whose exact bytes the archive holds")
    parser.add_argument("--restore", metavar="SESSION", help="Write an archived conversation's original files back")
    args = parser.parse_args()
    if args.limit < 0 or (args.free_older_than is not None and args.free_older_than < 1):
        parser.error("--limit must be nonnegative and --free-older-than at least 1")
    if not args.source and not args.restore:
        parser.error("--source is required")
    client = configured_client()
    if client is None:
        parser.error("Set ONE_CONV_ARCHIVE_URL and ONE_CONV_ARCHIVE_TOKEN")
    if args.restore:
        print(json.dumps(restore(reader, client, args.restore)))
        return 0
    origin = origin_id(reader)
    lock = reader.STATE_DIR / SYNC_LOCK
    if not acquire_lock(lock):
        print(json.dumps({"skipped": "another sync is running"}))
        return 0
    state_path, started = reader.STATE_DIR / SYNC_STATE, time.time()
    result, failures, error = {}, [], None
    try:
        result, failures = sync(reader, client, origin, args, lock)
    except Exception as exception:
        error = f"{type(exception).__name__}: {exception}"
        raise
    finally:
        try:
            finished = time.time()
            state = {"requested": started, **read_json(state_path), **result, "started": started, "finished": finished,
                     "error": error, "failed": len(failures), "failures": failures[:5]}
            if not error and not failures:
                state["succeeded"] = finished
            write_json(state_path, state)
        finally:
            lock.unlink(missing_ok=True)
    print(json.dumps(result))
    return 1 if failures else 0


def sync(reader, client, origin, args, lock):
    existing = {row["key"]: row for row in client.list_conversations()}
    freed = {}
    for row in existing.values():
        if row.get("freed"):
            freed.setdefault((row["source"], row["cwd"], row["session"], row["locator"]), []).append(row)
    projects = [p for p in reader._native_projects(tuple(args.source)) if not args.project or args.project.lower() in p.cwd.lower()]
    handles = sorted((h for p in projects for h in reader._native_project_sessions(p)), key=lambda h: h.mtime, reverse=True)
    handles = handles[:args.limit] if args.limit else handles
    keyed = [(conversation_key(origin, h.source, h.project_cwd, h.uuid, reader._record_locator(h)), h) for h in handles]
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} sync of {len(handles)} conversations", file=sys.stderr)
    result = {"uploaded": 0, "unchanged": 0}
    failures = []
    stamps = load_stamps(reader)
    for key, handle in keyed:
        lock.touch()
        try:
            for row in freed.get(reader._record_identity(handle), ()):
                if str(handle.path) in client.read_conversation(row["key"], row["revision"])["originals"]:
                    # Its file holds only what was written after freeing; uploading it would hide the rest.
                    raise ArchiveError(f"{handle.path} was freed on {datetime.fromtimestamp(row['freed']):%Y-%m-%d} and "
                                       f"written again since. Move it away, run `uv run scripts/archive-sync.py --restore "
                                       f"{handle.uuid}`, then add back what the moved file holds.")
            archived = existing.get(key)
            # The listed time covers only the main file; the stamp also covers a Cursor CLI
            # write-ahead log and every file in a Claude session folder.
            stamp = source_stamp(reader, handle, source_paths(reader, handle))
            # The cached stamp counts only while the archive still holds the revision it acknowledged:
            # a rolled-back or different archive must be reconciled, not skipped.
            if (archived and archived["mtime"] == handle.mtime and archived["capture_version"] == CAPTURE_VERSION
                    and stamps.get(key) == {"stamp": stamp, "revision": archived["revision"]}):
                result["unchanged"] += 1  # Avoid re-reading and re-encoding unchanged history.
                continue
            manifest, chunks = pack(capture(reader, handle, origin))
            head = revision(manifest)
            if archived and archived["revision"] == head:
                stamps[key] = {"stamp": stamp, "revision": head}
                result["unchanged"] += 1
                continue
            receipt = client.put(manifest, chunks, archived["revision"] if archived else None)
            if receipt != {"key": key, "revision": head}:
                raise ArchiveError("Archive acknowledgement does not match the uploaded capture")
            existing[key] = {"key": key, "revision": head, "mtime": handle.mtime}
            stamps[key] = {"stamp": stamp, "revision": head}
            result["uploaded"] += 1
        # One unreadable or malformed conversation must not stop the others.
        except SYNC_ERRORS + (reader.click.ClickException,) as error:
            failures.append(f"{handle.source}/{handle.uuid}: {type(error).__name__}: {error}")
            print(failures[-1], file=sys.stderr)
    save_stamps(reader, stamps)
    if args.free_older_than:
        result.update(free_older(reader, client, keyed, existing, time.time() - args.free_older_than * 86400, lock))
    if failures:
        result["failed"] = len(failures)
    return result, failures


def free_older(reader, client, keyed, existing, cutoff, lock):
    result = {"freed": 0, "freed_bytes": 0, "kept": 0}
    for key, handle in keyed:
        archived = existing.get(key)
        if handle.source not in FREEABLE_SOURCES or handle.mtime >= cutoff or not archived or archived["mtime"] != handle.mtime:
            continue
        lock.touch()
        try:
            result["freed_bytes"] += free(reader, client, handle, archived)
        except SYNC_ERRORS + (reader.click.ClickException,) as error:  # Keeping one is safe; report and go on.
            result["kept"] += 1
            print(f"{handle.source}/{handle.uuid}: kept: {error}", file=sys.stderr)
            continue
        result["freed"] += 1
    return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ArchiveError, OSError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
