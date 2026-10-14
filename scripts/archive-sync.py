#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click==8.3.3"]
# ///
"""Copy native conversations into an archive. Native files change only through --free-older-than and --restore."""

import argparse
import base64
from dataclasses import asdict
from datetime import datetime
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from one_conv.archive import (FREED, SYNC_LOCK, SYNC_STALE_SECONDS, SYNC_STATE, ArchiveError, configured_client,
                              conversation_key, digest, encode, pack, read_json, revision, split, write_json)

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
        paths.extend(sorted(p for p in (handle.path.parent / handle.uuid).rglob("*") if p.is_file()))
    if handle.source == "cursor-cli":
        paths.append(handle.path.parent / "meta.json")
    return paths


def fingerprint(paths):
    files = [p for path in paths for p in (path, Path(str(path) + "-wal"))]
    return [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files if p.exists()]


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
        if path.suffix == ".db":
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(":memory:") as snapshot:
                source.backup(snapshot)
                data = snapshot.serialize()
        else:
            data = path.read_bytes()
        result[str(path)] = data
    return result


def capture(reader, handle, origin):
    paths = source_paths(reader, handle)
    before = fingerprint(paths)
    originals = original_records(reader, handle, paths)
    account_label = None
    if handle.source in reader.CLOUD_SOURCES or (handle.path and handle.path.suffix == ".json"):
        cached = json.loads(handle.path.read_text()) if handle.path else {}
        if cached.get("complete") is False:
            raise ArchiveError(f"Incomplete provider capture: {handle.uuid}; pull complete history first")
        account_label = cached.get("account_label")
    turns = reader._handle_iter_turns(handle, include_subagents=False)
    expanded = reader._handle_iter_turns(handle, include_subagents=True) if handle.source == "claude" else None
    summary = reader._handle_summary(handle, {})
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
    return document


FREEABLE_SOURCES = ("claude", "codex", "omp")  # One file or folder per conversation; never a shared database.


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


def free(reader, client, handle, archived):
    """Delete a conversation's native files only if the archive holds exactly these bytes; return the bytes freed."""
    paths = source_paths(reader, handle)
    before = fingerprint(paths)
    originals = client.read_conversation(archived["key"], archived["revision"])["originals"]
    parts = {path: [name for name, _ in split(path.read_bytes())] for path in paths}
    if any(originals.get(str(path)) != names for path, names in parts.items()):
        raise ArchiveError("its files differ from the archived capture")
    if client.request("chunks/missing", {"digests": [name for names in parts.values() for name in names]})["missing"]:
        raise ArchiveError("the archive is missing some of its content")
    if source_paths(reader, handle) != paths or fingerprint(paths) != before:
        raise ArchiveError("it changed while being verified")
    size = sum(path.stat().st_size for path in paths)
    for path in paths:
        path.unlink()
    folder = handle.path.parent / handle.uuid
    for directory in sorted((d for d in folder.rglob("*") if d.is_dir()), reverse=True) + [folder]:
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return size


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
    """Write the newest archived copy of a conversation back; every target is checked before anything is written."""
    rows = [row for row in client.list_conversations() if row["source"] in FREEABLE_SOURCES and row["session"].startswith(session)]
    matches = {(row["source"], row["session"]) for row in rows}
    if len(matches) != 1:
        raise ArchiveError(f"{len(matches)} archived conversations match {session!r}; give the full session ID")
    # Another machine's newer copy may name paths that do not exist here; take the newest this machine can hold.
    for row in sorted(rows, key=lambda row: row["mtime"], reverse=True):
        manifest = client.read_conversation(row["key"], row["revision"])
        try:
            targets = {restore_target(history_roots(reader, row["source"]), name): parts
                       for name, parts in manifest["originals"].items()}
            break
        except ArchiveError as error:
            refused = error
    else:
        raise refused
    contents = {path: client.read_chunks(parts) for path, parts in targets.items()}
    for path, data in contents.items():
        if path.exists() and path.read_bytes() != data:
            raise ArchiveError(f"{path} exists with different content; move it away to restore")
    restored = 0
    for path, data in contents.items():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
            restored += 1
    main_file = next(iter(contents))  # Captures list the conversation's main file first.
    os.utime(main_file, (manifest["mtime"], manifest["mtime"]))  # Native and archived copies compare equal.
    freed_path = reader.STATE_DIR / FREED
    freed = read_json(freed_path)
    write_json(freed_path, {key: entry for key, entry in freed.items()
                            if (entry.get("source"), entry.get("session")) != (row["source"], row["session"])})
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
    freed = read_json(reader.STATE_DIR / FREED)
    projects = [p for p in reader._native_projects(tuple(args.source)) if not args.project or args.project.lower() in p.cwd.lower()]
    handles = sorted((h for p in projects for h in reader._native_project_sessions(p)), key=lambda h: h.mtime, reverse=True)
    handles = handles[:args.limit] if args.limit else handles
    keyed = [(conversation_key(origin, h.source, h.project_cwd, h.uuid, reader._record_locator(h)), h) for h in handles]
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} sync of {len(handles)} conversations", file=sys.stderr)
    result = {"uploaded": 0, "unchanged": 0}
    failures = []
    for key, handle in keyed:
        lock.touch()
        try:
            if key in freed:
                # Its new files hold only what was written after freeing; uploading them would hide the rest.
                raise ArchiveError(f"freed here and written again since; move its new files away and run "
                                   f"archive-sync.py --restore {handle.uuid}")
            archived = existing.get(key)
            if archived and archived["mtime"] == handle.mtime and archived["capture_version"] == CAPTURE_VERSION:
                result["unchanged"] += 1  # Avoid re-reading and re-encoding unchanged history.
                continue
            manifest, chunks = pack(capture(reader, handle, origin))
            head = revision(manifest)
            if archived and archived["revision"] == head:
                result["unchanged"] += 1
                continue
            receipt = client.put(manifest, chunks, archived["revision"] if archived else None)
            if receipt != {"key": key, "revision": head}:
                raise ArchiveError("Archive acknowledgement does not match the uploaded capture")
            existing[key] = {"key": key, "revision": head, "mtime": handle.mtime}
            result["uploaded"] += 1
        except Exception as error:  # One unreadable conversation must not stop the others.
            failures.append(f"{handle.source}/{handle.uuid}: {type(error).__name__}: {error}")
            print(failures[-1], file=sys.stderr)
    if args.free_older_than:
        result.update(free_older(reader, client, keyed, existing, freed, time.time() - args.free_older_than * 86400, lock))
    if failures:
        result["failed"] = len(failures)
    return result, failures


def free_older(reader, client, keyed, existing, freed, cutoff, lock):
    result = {"freed": 0, "freed_bytes": 0, "kept": 0}
    for key, handle in keyed:
        archived = existing.get(key)
        if handle.source not in FREEABLE_SOURCES or handle.mtime >= cutoff or not archived or archived["mtime"] != handle.mtime:
            continue
        lock.touch()
        try:
            result["freed_bytes"] += free(reader, client, handle, archived)
        except Exception as error:  # Keeping a conversation is safe; report why and continue.
            result["kept"] += 1
            print(f"{handle.source}/{handle.uuid}: kept: {error}", file=sys.stderr)
            continue
        result["freed"] += 1
        freed[key] = {"source": handle.source, "session": handle.uuid, "freed": time.time()}
        write_json(reader.STATE_DIR / FREED, freed)
    return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ArchiveError, OSError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
