"""Versioned archive transport and capture format, shared by the client and the server."""

from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import zlib

CHUNK_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024 * 1024
FIELDS = ("turns", "turns_with_subagents", "search_text")
DIGEST = re.compile(r"[0-9a-f]{64}")
READ_WORKERS = 4
# Files in one-conv's state directory shared by the sync script and readers.
SYNC_STATE = "archive-sync.json"
SYNC_LOCK = "archive-sync.lock"
SYNC_LOG = "archive-sync.log"
LISTING_CACHE = "archive-listing.json"
# Python's socket timeout bounds a whole send, so upload deadlines grow with the body,
# assuming at least 1 Mbit/s of upstream bandwidth.
MIN_UPLOAD_BYTES_PER_SECOND = 128 * 1024


class ArchiveError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to another endpoint.


def encode(value, *, sort_keys=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def revision(manifest):
    return digest(encode(manifest, sort_keys=True))


def conversation_key(origin, source, cwd, session, locator):
    return revision([origin, source, cwd, session, locator])


def split(data):
    """Content-addressed chunks of one field or original record, in order."""
    return [(digest(part), part) for part in (data[start:start + CHUNK_BYTES] for start in range(0, len(data), CHUNK_BYTES))]


def pack(capture):
    """Split a capture into a manifest and content-addressed raw chunks.

    Each field and original record is chunked separately, so an append reuses
    every chunk before the changed tail.
    """
    chunks = {}

    def record(data):
        parts = split(data)
        chunks.update(parts)
        return [name for name, _ in parts]

    manifest = {name: value for name, value in capture.items() if name not in (*FIELDS, "originals")}
    manifest["fields"] = {field: record(encode(capture[field])) for field in FIELDS}
    manifest["originals"] = {name: record(data) for name, data in capture["originals"].items()}
    return manifest, chunks


def compress(data):
    return zlib.compress(data)


def decompress(data, expected_digest):
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(data, CHUNK_BYTES)
    except zlib.error as error:
        raise ArchiveError("Chunk is not valid zlib data") from error
    if inflater.unconsumed_tail or inflater.unused_data or not inflater.eof or digest(raw) != expected_digest:
        raise ArchiveError("Chunk does not match its digest")
    return raw


class ArchiveClient:
    def __init__(self, url, token, timeout=60, max_request_bytes=MAX_REQUEST_BYTES):
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ArchiveError("Archive requires HTTPS (HTTP is allowed only on loopback).")
        if not token or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ArchiveError("Configure an archive URL and a separate token.")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.max_request_bytes = max_request_bytes

    def request(self, path, body=None, *, raw=False):
        data = encode(body) if body is not None else None
        request = Request(self.url + "/v1/" + path, data=data,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json"})
        timeout = self.timeout + len(data or b"") / MIN_UPLOAD_BYTES_PER_SECOND
        try:
            with build_opener(NoRedirect).open(request, timeout=timeout) as response:
                return response.read() if raw else json.load(response)
        except HTTPError as error:
            try:
                reason = json.load(error)["error"]
            except (OSError, ValueError, KeyError, TypeError):
                reason = error.reason
            raise ArchiveError(f"Archive returned HTTP {error.code} ({reason}); no changes acknowledged.") from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise ArchiveError("Archive unavailable or returned an invalid response.") from error

    def list_conversations(self, query=None):
        after = ""
        while True:
            page = self.request("conversations?" + urlencode({"after": after, **({"query": query} if query is not None else {})}))
            yield from page["items"]
            following = page["next"]
            if following is None:
                break
            if following <= after:
                raise ArchiveError("Archive pagination did not advance.")
            after = following

    def read_conversation(self, key, revision=None):
        """The capture's manifest; fetch content with read_field or read_capture."""
        return self.request("conversations/" + key + ("?" + urlencode({"revision": revision}) if revision else ""))

    def read_chunks(self, digests):
        with ThreadPoolExecutor(READ_WORKERS) as pool:
            return b"".join(pool.map(lambda d: decompress(self.request("chunks/" + d, raw=True), d), digests))

    def read_field(self, manifest, field):
        return json.loads(self.read_chunks(manifest["fields"][field]))

    def read_capture(self, key, revision=None):
        manifest = self.read_conversation(key, revision)
        capture = {name: value for name, value in manifest.items() if name not in ("fields", "originals")}
        capture.update({field: self.read_field(manifest, field) for field in FIELDS})
        capture["originals"] = {name: self.read_chunks(parts) for name, parts in manifest["originals"].items()}
        return capture

    def put(self, manifest, chunks, expected):
        """Upload missing chunks in bounded batches, then advance the conversation to this manifest."""
        missing = set()
        names = list(chunks)
        step = self.max_request_bytes // 70  # A digest costs 67 bytes in a JSON list.
        for start in range(0, len(names), step):
            missing.update(self.request("chunks/missing", {"digests": names[start:start + step]})["missing"])
        batches, batch, size = [], {}, 0
        for name in names:
            if name not in missing:
                continue
            data = base64.b64encode(compress(chunks[name])).decode()
            cost = len(name) + len(data) + 6  # Two quoted strings, a colon and a comma.
            if batch and size + cost > self.max_request_bytes - 16:
                batches.append(batch)
                batch, size = {}, 0
            batch[name] = data
            size += cost
        if batch:
            batches.append(batch)
        for batch in batches:  # Sequential: concurrent uploads would only share the same bandwidth.
            self.request("chunks", {"chunks": batch})
        return self.request("conversations", {"manifest": manifest, "expected": expected})


def configured_client():
    url = os.environ.get("ONE_CONV_ARCHIVE_URL")
    return ArchiveClient(url, os.environ.get("ONE_CONV_ARCHIVE_TOKEN", "")) if url else None
