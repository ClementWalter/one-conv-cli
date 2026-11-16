#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Copy a Python relay's archive.db into the Cloudflare relay, keeping every revision, head and origin.

    RELAY_ADMIN_TOKEN=... uv run relay-worker/scripts/import_archive.py --db archive.db --url https://RELAY

Chunks go through the archive API (/archive/v1/chunks, after a missing check, so the Worker
verifies each digest); revisions, heads (with their original change sequence and search text)
and the change generation go through /admin/archive/*, which the public API cannot express.
Every step skips what the relay already holds, so the import is idempotent: rerun it after an
interruption, or again after the last sync to the old relay, to copy only what is new.
The source database is opened read-only. A JSON summary of what was copied goes to stdout.
"""

import argparse
import base64
import json
import logging
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("import-archive")

# Below the archive's 16 MiB request limit, with room for JSON framing.
CHUNK_BATCH_BYTES = 12 * 1024 * 1024
# Well below the 32 MiB a Durable Object call carries; a single larger row goes alone.
ROW_BATCH_BYTES = 8 * 1024 * 1024
# Each chunk is one R2 write, and a Worker invocation has a bounded number of subrequests.
CHUNKS_PER_BATCH = 500
DIGEST_PAGE = 5000
UPLOADERS = 4


class Relay:
    def __init__(self, url: str, admin: str):
        self.url, self.admin = url.rstrip("/"), admin

    def post(self, path: str, body: dict) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode()
        request = Request(self.url + path, data=data, method="POST",
                          headers={"Authorization": f"Bearer {self.admin}", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=300) as response:
                return json.loads(response.read())
        except HTTPError as error:
            raise SystemExit(f"{path} refused ({error.code}): {error.read()[:300]!r}") from None


def batches(rows, size_of, limit: int, count: int = 10_000):
    """Consecutive rows grouped under `limit` bytes and `count` rows; a row larger than the limit is its own batch."""
    batch, size = [], 0
    for row in rows:
        cost = size_of(row)
        if batch and (size + cost > limit or len(batch) >= count):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += cost
    if batch:
        yield batch


def import_chunks(db: sqlite3.Connection, relay: Relay) -> int:
    """Upload the chunks the relay does not hold yet; returns how many were uploaded."""
    uploaded, last = 0, ""
    with ThreadPoolExecutor(UPLOADERS) as pool:
        while True:
            digests = [row[0] for row in db.execute(
                "SELECT digest FROM chunks_v1 WHERE digest > ? ORDER BY digest LIMIT ?", (last, DIGEST_PAGE))]
            if not digests:
                return uploaded
            last = digests[-1]
            missing = relay.post("/archive/v1/chunks/missing", {"digests": digests})["missing"]
            rows = ((digest, base64.b64encode(db.execute("SELECT data FROM chunks_v1 WHERE digest = ?", (digest,)).fetchone()[0]).decode())
                    for digest in missing)
            work = [pool.submit(relay.post, "/archive/v1/chunks", {"chunks": dict(batch)})
                    for batch in batches(rows, lambda row: len(row[0]) + len(row[1]) + 6, CHUNK_BATCH_BYTES,
                                             CHUNKS_PER_BATCH)]
            uploaded += sum(future.result()["stored"] for future in work)
            log.info("chunks: %d uploaded so far (through %s)", uploaded, last[:12])


def import_revisions(db: sqlite3.Connection, relay: Relay) -> int:
    added = 0
    rows = ({"key": key, "revision": revision, "manifest": manifest}
            for key, revision, manifest in db.execute("SELECT key, revision, manifest FROM revisions_v1 ORDER BY key, revision"))
    for batch in batches(rows, lambda row: len(row["manifest"]) + 200, ROW_BATCH_BYTES):
        added += relay.post("/admin/archive/revisions", {"revisions": batch})["added"]
        log.info("revisions: %d added so far", added)
    return added


def import_heads(db: sqlite3.Connection, relay: Relay) -> int:
    columns = {row[1] for row in db.execute("PRAGMA table_info(conversations_v1)")}
    # A database from before change sequences orders its heads by rowid, as the Python store does on upgrade.
    sequence = "change_sequence" if "change_sequence" in columns else "rowid"
    rows = ({"key": key, "revision": revision, "mtime": mtime, "metadata": metadata, "search_text": search_text,
             "change_sequence": change_sequence}
            for key, revision, mtime, metadata, search_text, change_sequence in db.execute(
                f"SELECT key, revision, mtime, metadata, search_text, {sequence} FROM conversations_v1 ORDER BY {sequence}"))
    added = 0
    for batch in batches(rows, lambda row: len(row["metadata"]) + len(row["search_text"].encode()) + 300, ROW_BATCH_BYTES):
        added += relay.post("/admin/archive/heads", {"heads": batch})["added"]
        log.info("heads: %d added so far", added)
    return added


def import_state(db: sqlite3.Connection, relay: Relay) -> dict:
    row = db.execute("SELECT sequence, generation FROM archive_state_v1 WHERE id = 1").fetchone()
    if row is None:
        return {}
    return relay.post("/admin/archive/state", {"sequence": row[0], "generation": row[1]})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="The Python relay's archive.db (opened read-only).")
    parser.add_argument("--url", default=os.environ.get("ONE_CONV_RELAY_URL"), help="The Worker's URL [env: ONE_CONV_RELAY_URL].")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    admin = os.environ.get("RELAY_ADMIN_TOKEN")
    if not args.url or not admin:
        log.error("set --url (or ONE_CONV_RELAY_URL) and RELAY_ADMIN_TOKEN")
        return 2
    relay = Relay(args.url, admin)
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        # Revisions before heads: a head is readable only once its manifest is there.
        summary = {"chunks": import_chunks(db, relay), "revisions": import_revisions(db, relay),
                   "heads": import_heads(db, relay), "state": import_state(db, relay)}
    except (URLError, OSError) as error:
        log.error("relay unreachable: %s; rerun to resume", error)
        return 1
    finally:
        db.close()
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
