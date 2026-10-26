"""Names agents choose for themselves, and topics several agents work on together.

A name is an alias for a session address; messages still route and reply by address, so
renaming never breaks a reply. A topic has members and a bounded message history; sending
to a topic delivers to every member. Files are keyed by a hash of the topic, never by the
raw text, and each member keeps a read position so catching up shows only what is new.
Posts are numbered in order; catch-up beyond its budget shows the newest whole and older ones
as the one-line block summaries agents write (see `summary`). When a hub is configured it holds
topic membership, history and summaries for every machine; this machine then keeps only each
reader's position.
"""

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import hubclient, summary
from .model import live_home

try:
    import fcntl
except ImportError:  # Windows has no flock; team files are then replaced unlocked.
    fcntl = None

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# A topic keeps this many recent messages; older ones fall out of catch-up.
TOPIC_HISTORY = 500
# Catch-up shows at most this many messages, so joining a busy topic stays readable.
CATCH_UP = 20


class TeamError(Exception):
    """A name or topic request that cannot be honoured as asked."""


def _check(kind: str, value: str) -> str:
    value = value.lstrip("@#").lower()
    if not NAME.fullmatch(value):
        raise TeamError(f"a {kind} is 1-32 of a-z, 0-9 and '-', starting with a letter or digit; got {value!r}")
    return value


def _root() -> Path:
    return live_home() / "team"


@contextmanager
def _locked():
    _root().mkdir(parents=True, exist_ok=True)
    with (_root() / ".lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read(path: Path, default):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write(path: Path, data) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(partial, path)


# --- names ------------------------------------------------------------------------------


def names() -> dict[str, str]:
    """name -> address."""
    return _read(_root() / "names.json", {})


def set_name(name: str, address: str, is_live, replace: bool = False) -> str:
    """Bind `name` to `address`. A name held by another session is refused, never taken silently.

    `replace` takes a name whose holder is not running; a running holder keeps it.
    """
    name = _check("name", name)
    with _locked():
        table = names()
        holder = table.get(name)
        if holder and holder != address:
            if is_live(holder):
                raise TeamError(f"@{name} belongs to {holder}, which is running; choose another name")
            if not replace:
                raise TeamError(f"@{name} belongs to {holder}, which is not running; "
                                "pass --replace to take it")
        # One name per session: choosing a new one releases the old.
        table = {n: a for n, a in table.items() if a != address}
        table[name] = address
        _write(_root() / "names.json", table)
    return name


def address_for(name: str) -> str | None:
    return names().get(_check("name", name))


def name_for(address: str) -> str | None:
    return next((n for n, a in names().items() if a == address), None)


# --- topics -----------------------------------------------------------------------------


def _topic_dir(topic: str) -> Path:
    return _root() / "topics" / hashlib.sha256(topic.encode()).hexdigest()[:20]


def _meta(topic: str) -> dict:
    return _read(_topic_dir(topic) / "meta.json", {})


def _messages(topic: str) -> list[dict]:
    rows = []
    try:
        lines = (_topic_dir(topic) / "messages.jsonl").read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A corrupt line must not hide the rest of the history.
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            # A post recorded before numbering is numbered by its place in the kept history.
            row.setdefault("seq", len(rows))
            rows.append(row)
    return rows


def _summaries(topic: str) -> dict[tuple[int, int], dict]:
    """(lo, hi) -> the settled summary row of that block."""
    out = {}
    try:
        lines = (_topic_dir(topic) / "summaries.jsonl").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A corrupt line must not hide the rest.
        if isinstance(row, dict) and isinstance(row.get("lo"), int) and isinstance(row.get("hi"), int):
            out.setdefault((row["lo"], row["hi"]), row)
    return out


def _texts(settled: dict) -> dict[tuple[int, int], str]:
    return {block: row["text"] for block, row in settled.items()}


def _post(row: dict) -> dict:
    return {"pos": row["seq"], "id": row["id"], "from": row["from"], "at": row["at"], "text": row["text"]}


def _posts(rows: list[dict]):
    """The kept posts in a block, as `summary` reads them."""
    return lambda lo, hi: [_post(r) for r in rows if lo <= r["seq"] < hi]


def _pending(topic: str, rows: list[dict], settled: dict) -> dict | None:
    """The summary this topic asks for next, or None when the tree is complete."""
    total = rows[-1]["seq"] + 1 if rows else 0
    todo = summary.pending(total, settled)
    if not todo:
        return None
    lo, hi = todo[0]
    return summary.request(topic, lo, hi, _texts(settled), _posts(rows), len(todo) - 1)


def _hub():
    try:
        return hubclient.configured()
    except hubclient.HubRefused as error:
        raise TeamError(str(error)) from None


def _hub_call(work):
    try:
        return work()
    except hubclient.HubRefused as error:
        if error.status == 403:
            raise TeamError(f"{error} (is `one-conv live serve` running, so this session is published?)") from None
        raise TeamError(str(error)) from None
    except hubclient.HubUnavailable as error:
        raise TeamError(f"{error}; topics are shared through the hub, so try again once it is reachable") from None


def members(topic: str) -> list[str]:
    topic = _check("topic", topic)
    hub = _hub()
    if hub is not None:
        rows = _hub_call(hub.topics)
        return sorted(next((row["members"] for row in rows if row.get("topic") == topic), []))
    return sorted(_meta(topic).get("members", {}))


def _remember_member(topic: str, address: str) -> None:
    """Keep this reader's position locally, whichever side holds the membership."""
    _topic_dir(topic).mkdir(parents=True, exist_ok=True)
    meta = _meta(topic) or {"topic": topic, "created": time.time(), "members": {}}
    meta["members"].setdefault(address, {"joined": time.time(), "seen": None})
    _write(_topic_dir(topic) / "meta.json", meta)


# A session started with its prompt can reach join_topic before its bridge's next publication
# (every 15 s) makes it known to the hub; the join waits that long for it.
PUBLISH_WAIT_SECONDS = 20.0


def _join_once_published(hub, topic: str, address: str, work: bool | None):
    deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
    while True:
        try:
            return hub.join(topic, address, work)
        except hubclient.HubRefused as error:
            if error.status != 403 or time.monotonic() >= deadline:
                raise
        time.sleep(1.0)


def join(topic: str, address: str, work: bool | None = None) -> str:
    """Join TOPIC; through a hub, `work` makes it the session's work channel (True) or only watches it (False)."""
    topic = _check("topic", topic)
    hub = _hub()
    if hub is not None:
        _hub_call(lambda: _join_once_published(hub, topic, address, work))
        with _locked():
            _remember_member(topic, address)
        return topic
    with _locked():
        _topic_dir(topic).mkdir(parents=True, exist_ok=True)
        meta = _meta(topic) or {"topic": topic, "created": time.time(), "members": {}}
        # A new member starts before the existing history, so catch-up shows it.
        meta["members"].setdefault(address, {"joined": time.time(), "seen": None})
        _write(_topic_dir(topic) / "meta.json", meta)
    return topic


def leave(topic: str, address: str) -> str:
    topic = _check("topic", topic)
    hub = _hub()
    if hub is not None:
        result = _hub_call(lambda: hub.leave(topic, address))
        if not result.get("removed"):
            raise TeamError(f"{address} is not a member of #{topic}")
        return topic
    with _locked():
        meta = _meta(topic)
        if address not in meta.get("members", {}):
            raise TeamError(f"{address} is not a member of #{topic}")
        del meta["members"][address]
        _write(_topic_dir(topic) / "meta.json", meta)
    return topic


def record(topic: str, message_id: str, sender: str, text: str) -> dict | None:
    """Append one exchanged message and keep the most recent TOPIC_HISTORY.

    Returns the summary request this post completes, if any, so the sender writes it.
    """
    topic = _check("topic", topic)
    with _locked():
        _topic_dir(topic).mkdir(parents=True, exist_ok=True)
        rows = _messages(topic)
        # Numbering continues past the kept history, so a position names one post forever.
        rows.append({"id": message_id, "seq": rows[-1]["seq"] + 1 if rows else 0, "at": time.time(),
                     "from": sender, "text": text})
        path = _topic_dir(topic) / "messages.jsonl"
        partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        partial.write_text("".join(json.dumps(r) + "\n" for r in rows[-TOPIC_HISTORY:]))
        os.replace(partial, path)
        meta = _meta(topic) or {"topic": topic, "created": time.time(), "members": {}}
        meta["last"] = time.time()
        meta.setdefault("members", {}).setdefault(sender, {"joined": time.time(), "seen": None})
        # The sender has seen their own message.
        meta["members"][sender]["seen"] = message_id
        _write(_topic_dir(topic) / "meta.json", meta)
        return _pending(topic, rows, _summaries(topic))


def _hub_catch_up(hub, topic: str, address: str, everything: bool, peek: bool, limit: int = CATCH_UP) -> dict:
    """Catch up from the hub, resuming after this reader's last hub cursor.

    The cursor is the hub's own post sequence; message ids are unique only per sending
    machine, so they cannot mark a position. A stored position that is not a cursor
    shows the latest messages instead.
    """
    with _locked():
        meta = _meta(topic)
        cursor = None if everything else (meta.get("members", {}).get(address) or {}).get("hub_cursor")
    if not (isinstance(cursor, str) and cursor.isdigit()):
        cursor = None
    rows = [_hub_row(r) for r in _hub_call(lambda: hub.history(topic, cursor, limit=TOPIC_HISTORY))]
    shown, blocks, omitted, request = rows, [], 0, None
    # A hub from before numbering gives no positions; it is read as before, newest posts only.
    if len(rows) > limit and rows[0].get("pos") is not None:
        info = _hub_call(lambda: hub.summaries(topic))
        settled = {summary.parse_block(s["block"]): s["text"] for s in info["summaries"]}
        blocks, singles = summary.layout(info["count"], rows[0]["pos"], limit, settled)
        shown = [r for r in rows if r["pos"] in set(singles)]
        if singles and len(shown) < len(singles):
            # More unseen posts than one history page holds: read the ones to show by position.
            shown = [_hub_row(r) for r in _hub_call(lambda: hub.history(topic, None, len(singles), singles[0]))]
        omitted, request = info["count"] - rows[0]["pos"] - len(shown), info.get("pending")
    elif len(rows) > limit:
        shown, omitted = rows[-limit:], len(rows) - limit
    current = members(topic)
    is_member = address in current or any(m.endswith("/" + address) for m in current)
    newest = max((int(r["cursor"]) for r in rows + shown if r.get("cursor")), default=None)
    if newest is not None and not peek and is_member:
        with _locked():
            _remember_member(topic, address)
            meta = _meta(topic)
            meta["members"][address]["hub_cursor"] = str(newest)
            _write(_topic_dir(topic) / "meta.json", meta)
    return {"topic": topic, "messages": shown, "summaries": blocks, "omitted": omitted, "member": is_member,
            **({"summarize": request} if request else {})}


def _hub_row(r: dict) -> dict:
    return {"id": r["id"], "cursor": r.get("cursor"), "pos": r.get("pos"), "at": r.get("created") or 0,
            "from": r["from"], "text": r["text"], "mode": r.get("mode", "push"),
            # A tagged direct message keeps its one recipient, so it is not read as a broadcast.
            **({"to": r["to"]} if isinstance(r.get("to"), str) and not r["to"].startswith("#") else {}),
            **({"reported": True} if r.get("reported") is True else {})}


def catch_up(topic: str, address: str, everything: bool = False, peek: bool = False,
             limit: int = CATCH_UP) -> dict:
    """Messages this member has not seen yet (at most `limit`), oldest first.

    They are marked seen unless `peek` is set, which is how a viewer reads without
    moving anyone's position.
    """
    topic = _check("topic", topic)
    hub = _hub()
    if hub is not None:
        return _hub_catch_up(hub, topic, address, everything, peek, limit)
    with _locked():
        meta = _meta(topic)
        if not meta:
            raise TeamError(f"#{topic} does not exist; `one-conv live join {topic}` creates it")
        rows, settled = _messages(topic), _summaries(topic)
        seen = (meta.get("members", {}).get(address) or {}).get("seen")
        ids = [r["id"] for r in rows]
        start = 0 if everything or seen not in ids else rows[ids.index(seen)]["seq"] + 1
        unseen = [r for r in rows if r["seq"] >= start]
        shown, blocks = unseen, []
        if len(unseen) > limit:
            total = rows[-1]["seq"] + 1
            blocks, singles = summary.layout(total, start, limit, _texts(settled))
            shown = [r for r in rows if r["seq"] in set(singles)]
        if not peek and address in meta.get("members", {}) and rows:
            meta["members"][address]["seen"] = rows[-1]["id"]
            _write(_topic_dir(topic) / "meta.json", meta)
        request = _pending(topic, rows, settled) if blocks else None
    return {"topic": topic, "messages": [{"pos": r["seq"], **r} for r in shown], "summaries": blocks,
            "omitted": len(unseen) - len(shown), "member": address in meta.get("members", {}),
            **({"summarize": request} if request else {})}


def pending_request(topic: str) -> dict | None:
    """The block summary this topic waits for, with its material, or None when none is due."""
    topic = _check("topic", topic)
    hub = _hub()
    if hub is not None:
        return _hub_call(lambda: hub.summaries(topic)).get("pending")
    with _locked():
        if not _meta(topic):
            raise TeamError(f"#{topic} does not exist; `one-conv live join {topic}` creates it")
        return _pending(topic, _messages(topic), _summaries(topic))


def summarize(topic: str, block: str, text: str, address: str) -> dict:
    """Settle one block's summary, written by `address`; the first line recorded for a block stays.

    A block is accepted once every post in it was exchanged and, above the smallest size, once
    both halves are settled, so summaries are only ever written from settled text.
    """
    topic = _check("topic", topic)
    try:
        lo, hi = summary.parse_block(block)
        text = summary.check(text)
    except summary.SummaryError as error:
        raise TeamError(str(error)) from None
    hub = _hub()
    if hub is not None:
        return _hub_call(lambda: hub.summarize(topic, summary.label(lo, hi), text, address))
    with _locked():
        if not _meta(topic):
            raise TeamError(f"#{topic} does not exist; `one-conv live join {topic}` creates it")
        rows, settled = _messages(topic), _summaries(topic)
        total = rows[-1]["seq"] + 1 if rows else 0
        result = {"topic": topic, "block": summary.label(lo, hi)}
        if (lo, hi) in settled:
            result.update(settled=False, existing=settled[(lo, hi)]["text"])
        else:
            if hi > total:
                raise TeamError(f"#{topic} holds {total} post(s); block {block} is not complete yet")
            if hi - lo > summary.LEAF and not all(half in settled for half in summary.halves(lo, hi)):
                raise TeamError(f"block {block} waits for its halves; summarize them first")
            row = {"lo": lo, "hi": hi, "text": text, "by": address, "at": time.time()}
            with (_topic_dir(topic) / "summaries.jsonl").open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            settled[(lo, hi)] = row
            result["settled"] = True
        result["pending"] = _pending(topic, rows, settled)
    return result


def open_block(topic: str, block: str) -> dict:
    """One block a level down: its halves' summaries, or its posts for a block of the smallest size."""
    topic = _check("topic", topic)
    try:
        lo, hi = summary.parse_block(block)
    except summary.SummaryError as error:
        raise TeamError(str(error)) from None
    hub = _hub()
    if hub is not None:
        return _hub_call(lambda: hub.open_block(topic, summary.label(lo, hi)))
    with _locked():
        if not _meta(topic):
            raise TeamError(f"#{topic} does not exist; `one-conv live join {topic}` creates it")
        rows, settled = _messages(topic), _summaries(topic)
    if lo >= (rows[-1]["seq"] + 1 if rows else 0):
        raise TeamError(f"#{topic} has no post #{lo} yet")
    return {"topic": topic, **summary.opened(lo, hi, _texts(settled), _posts(rows))}


def topics() -> list[dict]:
    hub = _hub()
    if hub is not None:
        return [{"topic": row["topic"], "members": sorted(row.get("members", [])), "last": row.get("last"),
                 "messages": None} for row in _hub_call(hub.topics)]
    rows = []
    for meta_path in sorted((_root() / "topics").glob("*/meta.json")):
        meta = _read(meta_path, {})
        if meta.get("topic"):
            rows.append({"topic": meta["topic"], "members": sorted(meta.get("members", {})),
                         "last": meta.get("last"), "messages": len(_messages(meta["topic"]))})
    return sorted(rows, key=lambda row: row["last"] or 0, reverse=True)
