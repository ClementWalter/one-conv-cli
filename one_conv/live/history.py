"""Report this machine's same-machine agent messages to the hub as history, so the workspace shows them.

A message between two agents on one machine never passes through the hub: the sender's adapter
handles it directly and only the local log (`messages.jsonl`) records it. The bridge reports
those rows with their original id, time, target, topic tag and local outcome. The hub stores
them with that outcome already settled, so reporting never delivers or wakes anyone. Rows that went through the hub,
topic fan-outs and the terminal human's inbox are never reported.
"""

import json
import logging
import os
from pathlib import Path

from .hub import LOCAL_AGENT, MAX_BODY, MESSAGE_ID, REPORT_BATCH
from .model import live_home

logger = logging.getLogger(__name__)

# What a local adapter's answer means for the hub: accepted is the adapter's receipt, not a read.
# Any other answer (queued until a turn ends, …) is reported as unconfirmed.
OUTCOMES = {"delivered": "accepted", "stored FYI": "accepted"}
# Statuses that mean the row was not delivered by a local adapter at all.
ELSEWHERE = {"stored on the hub", "stored for human"}
# The hub's request limit, measured on the exact JSON body hubclient sends.
BATCH_BYTES = MAX_BODY
ENVELOPE = len(json.dumps({"messages": []}).encode())
# Enough to cover the whole local log (router.MESSAGE_LOG) twice over.
CURSOR_KEEP = 4000


def cursor_path() -> Path:
    return live_home() / "bridge" / "reported.json"


def _reported() -> list[str]:
    try:
        ids = json.loads(cursor_path().read_text())
    except (OSError, ValueError):
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def _save(ids: list[str]) -> None:
    path = cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        json.dump(ids[-CURSOR_KEEP:], handle)
    os.replace(partial, path)


def eligible(row: dict) -> bool:
    """A message one local agent's adapter delivered to another local agent."""
    return (isinstance(row.get("id"), str) and MESSAGE_ID.fullmatch(row["id"]) is not None
            and all(isinstance(row.get(key), str) and LOCAL_AGENT.fullmatch(row[key]) for key in ("from", "to"))
            and isinstance(row.get("status"), str) and row["status"] != "" and row["status"] not in ELSEWHERE
            and isinstance(row.get("text"), str) and row["text"] != ""
            and type(row.get("at")) in (int, float) and 0 < row["at"] < 32_503_680_000)


def pending() -> list[dict]:
    """Eligible local log rows not yet answered by the hub, oldest first."""
    try:
        lines = (live_home() / "messages.jsonl").read_text().splitlines()
    except OSError:
        return []
    done, rows = set(_reported()), []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A corrupt line must not hide the rest of the log.
        if isinstance(row, dict) and eligible(row) and row["id"] not in done:
            rows.append(row)
    return rows


def item(row: dict) -> dict:
    mode = "fyi" if row.get("mode") == "fyi" else "push"
    # An FYI never asks for a reply, whatever an older log line recorded.
    reply = "none" if mode == "fyi" else row.get("reply") or "expected"
    return {"id": row["id"], "from": row["from"], "to": row["to"], "topic": row.get("topic") or None,
            "mode": mode, "reply": reply, "text": row["text"],
            "at": row["at"], "outcome": OUTCOMES.get(row["status"], "unconfirmed")}


def report(client) -> dict:
    """Send one batch of the most recent pending rows; every id the hub answers for is never sent again.

    A batch holds at most REPORT_BATCH rows and BATCH_BYTES of encoded messages, sent oldest first.
    """
    batch, size, oversized = [], ENVELOPE, []
    for row in reversed(pending()):
        encoded = len(json.dumps(item(row)).encode())
        if ENVELOPE + encoded > BATCH_BYTES:
            oversized.append(row["id"])  # It can never fit a request; settle it here rather than retry forever.
            logger.warning("message %s is too large to report as history", row["id"])
            continue
        separator = 2 if batch else 0  # json.dumps joins list items with ", ".
        if len(batch) == REPORT_BATCH or size + separator + encoded > BATCH_BYTES:
            break
        batch.append(row)
        size += separator + encoded
    batch.reverse()
    if oversized:
        _save(_reported() + oversized)
    if not batch:
        return {"recorded": 0, "duplicate": 0, "conflict": 0, "refused": 0}
    answer = client.request("POST", "/v1/history", {"messages": [item(row) for row in batch]})
    counts = {"recorded": 0, "duplicate": 0, "conflict": 0, "refused": 0}
    sent = {row["id"] for row in batch}
    answered = []
    results = answer.get("results") if isinstance(answer, dict) else None
    for result in results if isinstance(results, list) else []:
        # Only a well-formed answer about an id in this batch settles it; anything else leaves ids pending.
        if not (isinstance(result, dict) and isinstance(result.get("id"), str) and isinstance(result.get("status"), str)
                and result["id"] in sent and result["status"] in counts):
            continue
        counts[result["status"]] += 1
        answered.append(result["id"])
        if result["status"] in ("conflict", "refused"):
            logger.warning("the hub kept no history for message %s: %s", result["id"],
                           result.get("error") or "its id already holds a different message")
    if answered:
        _save(_reported() + answered)
    return counts
