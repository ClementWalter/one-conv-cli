"""The transcript of a shared session, for a share made with `--transcript`, on the Python relay.

It is read from the relay's archive copy of the one session the grant names (NODE and session
id fixed at creation), never from a key the guest gives. Turns render as relay-worker/src/
transcript.ts renders them for the remote MCP: text cleaned of harness noise, and with `raw`
also thinking, tool calls and cut tool output. Search runs over the same rendered text of that
session alone, without `raw`, so hidden blocks are never searched.
"""

import json
import re

from one_conv.archive import decompress

SYSTEM_REMINDER = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")
TASK_NOTIFICATION = re.compile(r"<task-notification>[\s\S]*?</task-notification>")
COMMAND_WRAPPER = re.compile(r"<command-message>[\s\S]*?</command-message>\s*<command-name>([\s\S]*?)</command-name>")
# A long tool output keeps its start and its end, where errors land.
TOOL_RESULT_CHARS = 4000
# One turn never takes a whole page on its own.
TURN_CHARS = 20_000
# A page of turns holds at most this many characters, as a remote MCP read does.
READ_BUDGET = 60_000
SNIPPET = 240


class TranscriptMissing(Exception):
    """The archive holds no copy of the shared session yet."""


def clean_text(text: str) -> str:
    text = SYSTEM_REMINDER.sub("", text)
    text = TASK_NOTIFICATION.sub("", text)
    return COMMAND_WRAPPER.sub(r"\1", text).strip()


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} characters cut …]\n{text[-half:]}"


def _js_text(value) -> str:
    """String(value) as JavaScript writes it, so both relays render a block alike."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def _tool_result(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        text = "\n".join(part["text"] for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"] != "")
    else:
        text = _js_text(content)
    return f"← {_cut(text.strip(), TOOL_RESULT_CHARS)}"


def render_turn(turn: dict, raw: bool) -> str | None:
    """One turn's display text, or None when it holds nothing worth showing (a bare tool round-trip without `raw`)."""
    blocks = turn.get("blocks")
    if blocks is None:
        text = clean_text(turn.get("text") or "")
        return _cut(text, TURN_CHARS) if text else None
    parts = []
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            text = clean_text(_js_text(block.get("text")))
            if text:
                parts.append(text)
        elif raw and kind == "thinking":
            thinking = _js_text(block.get("thinking")).strip()
            if thinking:
                parts.append(f"[thinking] {thinking}")
        elif raw and kind == "tool_use":
            name = "?" if block.get("name") is None else _js_text(block.get("name"))
            arguments = json.dumps({} if block.get("input") is None else block["input"], separators=(",", ":"),
                                   ensure_ascii=False)
            parts.append(f"→ {name}({arguments})")
        elif raw and kind == "tool_result":
            parts.append(_tool_result(block))
    return _cut("\n".join(parts), TURN_CHARS) if parts else None


def render_turns(turns: list, raw: bool) -> list[dict]:
    """The turns that render to something, numbered in that order: `index` is what `offset` takes."""
    rendered = []
    for turn in turns:
        text = render_turn(turn, raw) if isinstance(turn, dict) else None
        if text is not None:
            rendered.append({"index": len(rendered), "ts": turn.get("ts", ""), "role": turn.get("role", ""), "text": text})
    return rendered


def page_start(turns: list[dict], limit: int, offset: int | None, around: str | None) -> int:
    """`offset` when given, else around the first turn containing `around`, else the last `limit` turns."""
    if offset is not None:
        return max(0, min(offset, len(turns)))
    if around:
        needle = around.lower()
        for turn in turns:
            if needle in turn["text"].lower() or turn["ts"].startswith(around):
                return max(0, turn["index"] - limit // 2)
    return max(0, len(turns) - limit)


def page(turns: list[dict], start: int, limit: int, budget: int = READ_BUDGET) -> dict:
    """At most `limit` turns from `start`, stopping once `budget` characters are used (always at least one turn)."""
    out, used = [], 0
    for turn in turns[start:]:
        if len(out) >= limit or (out and used + len(turn["text"]) > budget):
            break
        out.append(turn)
        used += len(turn["text"])
    end = start + len(out)
    return {"total": len(turns), "turns": out, "next_offset": end if end < len(turns) else None,
            "previous_offset": max(0, start - limit) if start > 0 else None}


def search(turns: list[dict], query: str, limit: int) -> dict:
    """Turns holding every word of `query` (any order, any case), newest first, with a snippet around the first word."""
    words = query.lower().split()
    hits = [turn for turn in turns if words and all(word in turn["text"].lower() for word in words)]
    out = []
    for turn in reversed(hits[-limit:] if limit else []):
        at = turn["text"].lower().find(words[0])
        start = max(0, at - SNIPPET // 2)
        out.append({"index": turn["index"], "ts": turn["ts"], "role": turn["role"],
                    "snippet": turn["text"][start:start + SNIPPET]})
    return {"hits": out, "total_hits": len(hits)}


def session_id(address: str) -> str:
    """The session id of a local address harness:id, as the archive's metadata names it."""
    return address.partition(":")[2]


def archived(store, node: str, address: str) -> dict | None:
    """{key, mtime} of the newest archived copy of NODE/address in `store`, or None."""
    with store.engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT key, mtime FROM conversations_v1 WHERE json_extract(metadata, '$.session') = ?"
            " AND json_extract(metadata, '$.origin') = ? ORDER BY mtime DESC LIMIT 1",
            (session_id(address), node)).first()
    return {"key": row[0], "mtime": row[1]} if row else None


def load(store, node: str, address: str) -> tuple[list, float]:
    """(turns, archived mtime) of the shared session's archived copy; TranscriptMissing when there is none."""
    head = archived(store, node, address)
    manifest = store.read_conversation(head["key"]) if head else None
    if manifest is None:
        raise TranscriptMissing(f"the relay's archive holds no copy of {node}/{address} yet; its bridge syncs it every minute")
    data = b"".join(decompress(store.read_chunk(digest), digest) for digest in manifest["fields"]["turns"])
    return json.loads(data), head["mtime"]
