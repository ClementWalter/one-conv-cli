"""The board: notes on shared topics that agents and the person post on the live relay and pull later.

Nothing on the board is pushed: a post wakes nobody and files no inbox note. Each reader keeps
its read positions on its own machine, in `<live home>/board/read.json` as
{reader: {topic: seq}}, so reading is a cheap pull of what is past that seq. A reader is a
session address, an explicit `--as` name, or `user` for the person. A tag filter keeps its own
position ("topic tag ..."), so reading one tag leaves the rest unread.

The board needs a relay: it is how machines share it, so without one every call refuses.
"""

import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from .live import hubclient
from .live.hub_board import FILE_LIMIT, FILES_PER_POST, RESERVED, TEXT_LIMIT, TOPIC
from .live.model import live_home

try:
    import fcntl
except ImportError:  # Windows has no flock; positions are then replaced unlocked.
    fcntl = None

logger = logging.getLogger(__name__)

TAG = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
TAGS_PER_POST = 8
PAGE = 200
READER_LIMIT = 200
USER = "user"
# Retries of one post within this window reuse its id, so the relay stores it once.
IDEMPOTENCY_WINDOW_SECONDS = 600
# A board file may be 25 MB: give its transfer more than a message's time.
TRANSFER_TIMEOUT = 300
NO_RELAY = ("The board lives on the live relay, and this machine has no relay configured. Connect it with "
            "`one-conv live join` (or `live serve --hub ...`); see `one-conv live --help`.")


class BoardError(Exception):
    """A board request that cannot go through, in words the caller can act on."""


def client() -> hubclient.HubClient:
    try:
        hub = hubclient.configured()
    except hubclient.HubRefused as error:
        raise BoardError(str(error)) from None
    if hub is None:
        raise BoardError(NO_RELAY)
    return hub


def _call(method: str, path: str, body: dict | None = None) -> dict:
    try:
        return client().request(method, path, body)
    except (hubclient.HubRefused, hubclient.HubUnavailable) as error:
        raise BoardError(str(error)) from None


def check_topic(topic: str) -> str:
    if not TOPIC.fullmatch(topic) or topic in RESERVED:
        raise BoardError(f"A topic is 1 to 48 lowercase letters, digits and dashes (not {', '.join(RESERVED)}), "
                         f"got {topic!r}.")
    return topic


def check_tags(tags) -> list[str]:
    unique = list(dict.fromkeys(tags))
    if len(unique) > TAGS_PER_POST or not all(TAG.fullmatch(t) for t in unique):
        raise BoardError(f"Tags are at most {TAGS_PER_POST} lowercase words (letters, digits, dashes), "
                         f"got {', '.join(unique)}.")
    return unique


# --- who reads and who posts ---------------------------------------------------------------------

def reader_for(explicit: str | None, session) -> str:
    """The reader whose positions move: `--as`, else the calling session, else the person."""
    if explicit:
        if len(explicit) > READER_LIMIT or any(char.isspace() for char in explicit):
            raise BoardError(f"--as is a reader name without spaces, at most {READER_LIMIT} characters.")
        return explicit
    if session is not None and not session.id.startswith("?"):
        return session.address
    return USER


def author_for(session, origin: str) -> tuple[str, str]:
    """(from, name) of a post: the relay vouches only for this node's published sessions and the person."""
    from .live import names
    if session is not None and not session.id.startswith("?"):
        handle = names.name_for(session.address)
        return session.address, f"@{handle}" if handle else (session.title or "")
    return (USER, "") if origin == USER else ("unknown", "" if origin == "unknown" else origin)


# --- read positions ----------------------------------------------------------------------------------

def _positions_path() -> Path:
    return live_home() / "board" / "read.json"


@contextmanager
def _positions():
    """Every reader's positions, locked for the block and written back after it."""
    path = _positions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".read.lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            partial = path.with_name(f".read.{os.getpid()}.tmp")
            partial.write_text(json.dumps(data, indent=1, sort_keys=True))
            os.replace(partial, path)


def positions(reader: str) -> dict[str, int]:
    with _positions() as data:
        found = data.get(reader)
        return dict(found) if isinstance(found, dict) else {}


def _mine(data: dict, reader: str) -> dict:
    """This reader's positions inside the locked data, replacing an entry that is not a mapping."""
    if not isinstance(data.get(reader), dict):
        data[reader] = {}
    return data[reader]


def _key(topic: str, tags: list[str]) -> str:
    return " ".join([topic, *sorted(tags)])


# --- operations --------------------------------------------------------------------------------------

def topics(reader: str) -> list[dict]:
    """Every topic, most recent first, with `unread` past this reader's position and `known` if it read or posted there."""
    known = {key: seq for key, seq in positions(reader).items() if " " not in key and isinstance(seq, int)}
    query = urlencode([("read", f"{topic}:{seq}") for topic, seq in sorted(known.items())])
    rows = _call("GET", "/v1/board" + (f"?{query}" if query else "")).get("topics", [])
    return [{**row, "known": row.get("topic") in known} for row in rows]


def _page(topic: str, after: int, tags: list[str], limit: int) -> dict:
    query = urlencode([("after", max(0, after)), ("limit", limit), *(("tag", t) for t in tags)])
    return _call("GET", f"/v1/board/{check_topic(topic)}?{query}")


def read(topic: str, reader: str, tags=(), limit: int = 50, peek: bool = False, show_all: bool = False,
         after: int | None = None) -> dict:
    """Posts past the reader's position (or `after`), oldest first, then marked read unless `peek`.

    `show_all` returns the newest `limit` posts, read or not, and moves nothing. An explicit
    `after` reads from there and moves nothing either.
    """
    tags, limit = check_tags(tags), max(1, min(limit, PAGE))
    key = _key(check_topic(topic), tags)
    if show_all:
        newest, cursor = [], 0
        while True:
            page = _page(topic, cursor, tags, PAGE)
            newest = (newest + page["posts"])[-limit:]
            if len(page["posts"]) < PAGE:
                return {**page, "posts": newest, "reader": reader}
            cursor = page["posts"][-1]["seq"]
    with _positions() as data:
        mine = _mine(data, reader)
        start = after if after is not None else int(mine.get(key, 0))
        page = _page(topic, start, tags, limit)
        if not peek and after is None:
            # A full page may stop short of the newest post; otherwise everything up to `last` was seen.
            seen = page["posts"][-1]["seq"] if len(page["posts"]) >= limit else max(page["last"], start)
            mine[key] = max(seen, start)
    return {**page, "reader": reader}


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def plan(topic: str, text: str, tags=(), files=()) -> dict:
    """The checked post before anything is sent: what a dry run shows and what `post` sends."""
    check_topic(topic)
    tags = check_tags(tags)
    if len(text.encode("utf-8", "surrogatepass")) > TEXT_LIMIT:
        raise BoardError(f"A post's text is at most {TEXT_LIMIT} bytes; attach longer material as a file.")
    paths = [Path(p).expanduser() for p in files]
    if len(paths) > FILES_PER_POST:
        raise BoardError(f"A post carries at most {FILES_PER_POST} files.")
    if not text.strip() and not paths:
        raise BoardError("A post needs text or a file.")
    attached = []
    for path in paths:
        if not path.is_file():
            raise BoardError(f"{path} is not a file.")
        size = path.stat().st_size
        if not 0 < size <= FILE_LIMIT:
            raise BoardError(f"{path} is {size} bytes; a file is 1 byte to {FILE_LIMIT // 1_000_000} MB.")
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        attached.append({"path": str(path), "name": path.name, "size": size, "content_type": kind,
                         "sha256": _digest(path)})
    return {"topic": topic, "text": text, "tags": tags, "files": attached}


def post_id(author: str, prepared: dict, title: str, at: float) -> str:
    """The same post from the same author within the window gets the same id, so a retry is stored once."""
    files = [[f["name"], f["sha256"]] for f in prepared["files"]]
    payload = [author, prepared["topic"], title, prepared["text"], prepared["tags"], files,
               int(at // IDEMPOTENCY_WINDOW_SECONDS)]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:32]


def post(prepared: dict, author: str, name: str, reader: str, title: str = "") -> dict:
    """Upload the files, then the post; the poster's own post does not count as unread for it."""
    hub, topic = client(), prepared["topic"]
    uploaded = []
    for file in prepared["files"]:
        try:
            meta = hub.upload(file["content_type"], Path(file["path"]).read_bytes(), f"/v1/board/{topic}/files",
                              {"X-File-Name": quote(file["name"], safe="")}, timeout=TRANSFER_TIMEOUT)
        except (hubclient.HubRefused, hubclient.HubUnavailable) as error:
            raise BoardError(f"{file['name']}: {error}") from None
        uploaded.append(meta["id"])
    body = {"id": post_id(author, prepared, title, time.time()), "from": author, "name": name,
            "text": prepared["text"], "tags": prepared["tags"], "files": uploaded,
            **({"title": title} if title else {})}
    receipt = _call("POST", f"/v1/board/{topic}", body)
    with _positions() as data:
        mine = _mine(data, reader)
        current = int(mine.get(topic, 0))
        # Caught up before posting: the reader stays caught up. Otherwise the topic is still known to it.
        mine[topic] = max(current, receipt["seq"]) if current >= receipt.get("previous", 0) else current
    return {**receipt, "from": author, "files": uploaded}


def find_file(topic: str, file_id: str) -> dict:
    """The metadata a post lists for `file_id`, paging through the topic."""
    cursor = 0
    while True:
        page = _page(topic, cursor, [], PAGE)
        for row in page["posts"]:
            for file in row.get("files", []):
                if file.get("id") == file_id:
                    return {**file, "post": row["id"]}
        if len(page["posts"]) < PAGE:
            raise BoardError(f"No live post on {topic} carries file {file_id}.")
        cursor = page["posts"][-1]["seq"]


def get_file(topic: str, file_id: str) -> tuple[dict, bytes]:
    """A file's metadata and bytes, checked against the sha256 its post lists."""
    meta = find_file(check_topic(topic), file_id)
    try:
        _, data = client().download(f"/v1/board/{topic}/files/{file_id}", timeout=TRANSFER_TIMEOUT)
    except (hubclient.HubRefused, hubclient.HubUnavailable) as error:
        raise BoardError(str(error)) from None
    if hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise BoardError(f"{meta['name']} arrived with another sha256 than its post lists; nothing was written.")
    return meta, data


def search(query: str, topic: str | None = None, all_words: bool = False, limit: int = 20) -> list[dict]:
    if topic:
        check_topic(topic)
    params = [("q", query), ("limit", max(1, min(limit, PAGE)))]
    params += ([("topic", topic)] if topic else []) + ([("words", "1")] if all_words else [])
    return _call("GET", f"/v1/board/search?{urlencode(params)}").get("posts", [])


def remove(topic: str, post: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", post):
        raise BoardError("A post id is 32 lowercase hex characters (see `board read --json`).")
    return _call("DELETE", f"/v1/board/{check_topic(topic)}/posts/{post}")


def unread_lines(reader: str) -> list[str]:
    """One line per topic this reader read or posted in that has new posts; nothing without a relay or positions."""
    if not any(" " not in key for key in positions(reader)):
        return []
    try:
        rows = topics(reader)
    except BoardError as error:
        logger.info("board unavailable for the feed: %s", error)
        return []
    return [f"board: {row['unread']} new post{'s' if row['unread'] != 1 else ''} in {row['topic']}"
            for row in rows if row["known"] and row["unread"]]
