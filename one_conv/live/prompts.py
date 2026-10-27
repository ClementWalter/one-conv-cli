"""Mirror a terminal session's conversation to the hub: the human's prompts and the session's replies.

The workspace shows one-conv messages, so words sent from the phone reach it while the same words typed
at the laptop's keyboard, and what the session answered there, never did. The bridge runs
`one-conv report-prompts` every PROMPT_SYNC_SECONDS: for each running session on this machine it reads
the transcript's new turns as `one-conv local thread` renders them (prose only), keeps the human's typed
prompts (a relayed one-conv envelope is already a message; tool results, slash-command wrappers, echoed
command output and the prompt a spawned session was started with are not the human) and the session's
text replies, and reports them to the hub as history between `user` and the session, at each turn's own
time, so the workspace shows the exchange in that session's conversation and wakes nobody. Positions are
kept per session as the feed keeps its own, so a rewritten transcript is noticed instead of replayed.
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .. import feed
from .hub import MAX_TEXT, REPORT_BATCH
from .model import live_home

logger = logging.getLogger(__name__)

PROMPT_SYNC_SECONDS = 20.0
# Turns reported when a session is first seen: its recent past, not its whole life.
FIRST_SIGHT = 40
# A user turn carrying one of these was not typed as a sentence: a relayed one-conv message (already on the
# hub) or Claude Code's slash-command plumbing and the local command output it echoes back.
NOT_TYPED = re.compile(r"<\s*(one-conv-message|command-name|command-message|local-command-caveat|local-command-stdout)\b"
                       r"|\A\s*\[Image: source: [^\]]*\]\s*\Z", re.IGNORECASE)


def cursor_path() -> Path:
    return live_home() / "bridge" / "prompts.json"


def load_state() -> dict:
    try:
        data = json.loads(cursor_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    path = cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        json.dump(state, handle)
    os.replace(partial, path)


def spawned_addresses() -> set[str]:
    """Sessions another agent or the hub started: their first prompt is the order they were given."""
    try:
        rows = json.loads((live_home() / "spawned.json").read_text())
    except (OSError, ValueError):
        return set()
    return {row["address"] for row in rows.values() if isinstance(row, dict) and row.get("address")} \
        if isinstance(rows, dict) else set()


def not_typed(turn) -> bool:
    """A user turn that is a relayed message or command plumbing rather than something the human typed."""
    return any(isinstance(block, dict) and block.get("type") == "text" and NOT_TYPED.search(block.get("text") or "")
               for block in turn.blocks)


def epoch(ts: str) -> float:
    """A transcript's ISO time as epoch seconds; an unreadable one is taken as now."""
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return time.time()


def prompt_id(node: str, address: str, index: int, ts: str) -> str:
    """Stable per turn, so a prompt reported twice is a duplicate on the hub, never a second message."""
    return hashlib.sha256(json.dumps(["prompt", node, address, index, ts]).encode()).hexdigest()[:32]


def conversation(thread: feed.Thread, turns: list, start: int | None, skip_first: bool) -> list[dict]:
    """The typed prompts and text replies at raw index >= `start`, or the last FIRST_SIGHT when start is None.

    `skip_first` leaves out the session's first user turn, the order a spawned session was given.
    """
    raw_index = {id(turn): index for index, turn in enumerate(turns)}
    first = next((raw_index[id(t)] for t in turns if t.role == "user"), None)
    rows = []
    for turn in thread.visible(turns):
        index = raw_index.get(id(turn), -1)
        if start is not None and index < start:
            continue
        if turn.role == "user" and ((skip_first and index == first) or not_typed(turn)):
            continue
        text = thread.render(turn)
        if not text:
            continue
        # A prompt sent again, longer, before any answer replaces the interrupted one it extends.
        if rows and turn.role == "user" and rows[-1]["role"] == "user" and text.startswith(rows[-1]["text"]):
            rows.pop()
        rows.append({"index": index, "ts": turn.ts, "role": turn.role, "text": text})
    return rows if start is not None else rows[-FIRST_SIGHT:]


def item(node: str, address: str, row: dict) -> dict:
    """One turn as the hub records history: the human writes to the session, the session answers the human."""
    human = row["role"] == "user"
    return {"id": prompt_id(node, address, row["index"], row["ts"]), "from": "user" if human else address,
            "to": address if human else "user", "topic": None, "mode": "push", "reply": "none",
            "text": row["text"][:MAX_TEXT], "at": epoch(row["ts"]), "outcome": "accepted"}


def report(client, node: str, threads: dict[str, feed.Thread], state: dict | None = None) -> dict:
    """Report each running session's new prompts; a session's position moves once the hub answered for them all."""
    state = load_state() if state is None else state
    counts = {"recorded": 0, "duplicate": 0, "conflict": 0, "refused": 0, "unavailable": 0}
    spawned = spawned_addresses()
    for address, thread in threads.items():
        saved = state.get(address) if isinstance(state.get(address), dict) else None
        try:
            turns = feed.load_turns(thread, {"read": saved} if saved else {})
        except feed.Unavailable as error:
            logger.warning("%s: transcript unavailable: %s", address, error)
            counts["unavailable"] += 1
            continue
        start, _ = feed.locate(turns, saved)
        items = [item(node, address, row) for row in conversation(thread, turns, start, address in spawned)]
        answered = set()
        for offset in range(0, len(items), REPORT_BATCH):
            batch = items[offset:offset + REPORT_BATCH]
            answer = client.request("POST", "/v1/history", {"messages": batch})
            results = answer.get("results") if isinstance(answer, dict) else None
            for result in results if isinstance(results, list) else []:
                if isinstance(result, dict) and result.get("status") in counts and result.get("id") in {i["id"] for i in batch}:
                    counts[result["status"]] += 1
                    answered.add(result["id"])
                    if result["status"] in ("conflict", "refused"):
                        logger.warning("the hub kept no prompt %s: %s", result["id"], result.get("error") or "conflict")
        if answered >= {i["id"] for i in items}:
            state[address] = feed.position(turns, thread.stamp)
    save_state(state)
    return counts


def build_prompts_command(find_threads, live_addresses):
    """`one-conv report-prompts`, run by the bridge: report this machine's running sessions' new turns."""
    import click

    from . import hubclient

    @click.command("report-prompts", context_settings={"help_option_names": ["-h", "--help"]}, hidden=True, epilog="\b\nExamples:\n  one-conv report-prompts\n  one-conv report-prompts --json")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    def report_prompts(as_json):
        """Report the prompts typed into this machine's running sessions, and their replies, to the hub."""
        try:
            hub = hubclient.configured()
        except hubclient.HubRefused as error:
            raise click.ClickException(str(error)) from None
        if hub is None:
            raise click.ClickException("prompts are mirrored to a live hub, and this machine has none")
        threads, _ = find_threads(live_addresses())
        try:
            counts = report(hub, hub.node, threads)
        except (hubclient.HubRefused, hubclient.HubUnavailable, OSError) as error:
            raise click.ClickException(str(error)) from None
        if as_json:
            click.echo(json.dumps(counts))
        else:
            click.echo(", ".join(f"{value} {key}" for key, value in counts.items() if value) or "nothing new")

    return report_prompts
