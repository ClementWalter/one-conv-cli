#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click==8.3.3"]
# ///
"""Copy native conversations into an archive; never delete or modify source history."""

import argparse
import base64
from dataclasses import asdict
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from one_conv.archive import ArchiveError, configured_client, conversation_key, digest, encode, pack, revision

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
        "session": handle.uuid, "mtime": handle.mtime, "capture_version": CAPTURE_VERSION,
        "account_label": account_label, "summary": summary,
        "unread_native": handle.unread_native, "turns": [asdict(turn) for turn in turns],
        "turns_with_subagents": [asdict(turn) for turn in expanded] if expanded != turns and expanded is not None else None,
        "search_text": [text for turn in turns if (text := reader._render_turn(turn, raw=False))],
        "originals": originals}
    changed = original_records(reader, handle, paths) != originals if handle.source == "cursor" else fingerprint(paths) != before
    if changed:
        raise ArchiveError(f"Source changed during capture: {handle.uuid}; retry when idle")
    return document


def main():
    reader = load_reader()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=reader.SOURCES, action="append", required=True)
    parser.add_argument("--project", help="Restrict to project paths containing this text")
    parser.add_argument("--limit", type=int, default=0, help="Maximum conversations (0 = all)")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    client = configured_client()
    if client is None:
        parser.error("Set ONE_CONV_ARCHIVE_URL and ONE_CONV_ARCHIVE_TOKEN")
    origin = origin_id(reader)
    existing = {row["key"]: row for row in client.list_conversations()}
    projects = [p for p in reader._native_projects(tuple(args.source)) if not args.project or args.project.lower() in p.cwd.lower()]
    handles = sorted((h for p in projects for h in reader._native_project_sessions(p)), key=lambda h: h.mtime, reverse=True)
    uploaded = unchanged = failed = 0
    for handle in handles[:args.limit] if args.limit else handles:
        try:
            key = conversation_key(origin, handle.source, handle.project_cwd, handle.uuid)
            archived = existing.get(key)
            if archived and archived["mtime"] == handle.mtime and archived["capture_version"] == CAPTURE_VERSION:
                unchanged += 1  # Avoid re-reading and re-encoding unchanged history.
                continue
            manifest, chunks = pack(capture(reader, handle, origin))
            digest = revision(manifest)
            if archived and archived["revision"] == digest:
                unchanged += 1
                continue
            receipt = client.put(manifest, chunks, archived["revision"] if archived else None)
            if receipt != {"key": key, "revision": digest}:
                raise ArchiveError("Archive acknowledgement does not match the uploaded capture")
            existing[key] = {"revision": digest, "mtime": handle.mtime}
            uploaded += 1
        except Exception as error:  # One unreadable conversation must not stop the others.
            failed += 1
            print(f"{handle.source}/{handle.uuid}: {type(error).__name__}: {error}", file=sys.stderr)
    result = {"uploaded": uploaded, "unchanged": unchanged}
    if failed:
        result["failed"] = failed
    print(json.dumps(result))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ArchiveError, OSError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
