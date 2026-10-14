"""List live sessions across harnesses, identify the sender, resolve a target and deliver."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from . import claude, codex, cursor, omp
from .model import LiveSession, envelope, live_home, parse_address

logger = logging.getLogger(__name__)
ADAPTERS = {"claude": claude, "codex": codex, "omp": omp, "cursor": cursor}
# Two agents told to reply to each other would otherwise relay forever.
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 300


class LiveError(Exception):
    """A target that cannot be resolved or a delivery that must not happen."""


def list_all(harness: str | None = None) -> list[LiveSession]:
    sessions = []
    for name, adapter in ADAPTERS.items():
        if harness and harness != name:
            continue
        try:
            sessions += adapter.list_sessions()
        except (OSError, subprocess.SubprocessError) as error:
            # One broken harness must not hide the others' sessions.
            logger.warning("%s: cannot list live sessions: %s", name, error)
    return sessions


def _process(pid: int) -> tuple[int, str] | None:
    """(parent pid, executable name) of `pid`, or None once the process is gone."""
    result = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], check=False, capture_output=True, text=True)
    parts = result.stdout.strip().split(None, 1)
    if len(parts) != 2:
        return None
    return int(parts[0]), Path(parts[1]).name


def whoami(explicit: str | None = None, meta: dict | None = None, pid: int | None = None) -> LiveSession | None:
    """The session this process acts for; None means the user at a plain terminal.

    Environment variables leak into grandchildren (an omp started from Claude Code keeps
    Claude's session id), so the nearest harness process in the ancestry decides.
    """
    if explicit:
        harness, ident = parse_address(explicit)
        if not harness:
            raise LiveError(f"--from must be an address like claude:<id>, got {explicit!r}")
        known = next((s for s in list_all(harness) if s.id == ident), None)
        return known or LiveSession(harness, ident)
    thread_id = (meta or {}).get("threadId")
    if thread_id:
        return next((s for s in codex.list_sessions() if s.id == thread_id), LiveSession("codex", thread_id))
    current = pid or os.getpid()
    for _ in range(32):
        found = claude.session_for_pid(current) or omp.session_for_pid(current)
        if found:
            return found
        step = _process(current)
        if step is None:
            return None
        parent, name = step
        if name == "codex":
            ident = os.environ.get("CODEX_THREAD_ID")
            return LiveSession("codex", ident) if ident else LiveSession("codex", "?unknown")
        if name == "cursor-agent":
            return LiveSession("cursor", cursor.chat_for_pid(current) or "?unknown")
        if name in ("Cursor", "Cursor Helper (Plugin)"):
            # The IDE hosts every chat in one process, so the calling chat cannot be told apart.
            return LiveSession("cursor", "?unknown")
        if parent <= 1:
            return None
        current = parent
    return None


def resolve(target: str, sessions: list[LiveSession] | None = None,
            exclude: LiveSession | None = None) -> LiveSession:
    """Match an address, id prefix, then title/cwd substring; `exclude` drops the sender first."""
    harness, query = parse_address(target)
    if exclude is not None and harness in (None, exclude.harness) and exclude.id.startswith(query):
        raise LiveError(f"{exclude.address} is the sending session itself.")
    pool = [s for s in (list_all(harness) if sessions is None else sessions)
            if harness in (None, s.harness) and (exclude is None or s.address != exclude.address)]
    for matches in ([s for s in pool if s.id == query],
                    [s for s in pool if s.id.startswith(query)],
                    [] if harness else [s for s in pool if query.lower() in f"{s.title} {s.cwd}".lower()]):
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            listing = "\n".join(f"  {s.address}  {s.title or s.cwd}" for s in matches)
            raise LiveError(f"{target!r} matches several live sessions:\n{listing}")
    if harness == "codex":
        stored = codex.stored_session(query)
        if stored:
            return stored
    if harness == "cursor":
        return LiveSession("cursor", query, status="not seen recently", delivery="stop hook",
                           endpoint=str(cursor.inbox(query)))
    hint = " A closed Claude Code session can be continued with `one-conv local send`." if harness == "claude" else ""
    raise LiveError(f"No live session matches {target!r}. Run `one-conv live ls`.{hint}")


def _check_rate(sender: str, target: str) -> None:
    """Refuse a pair that already exchanged RATE_LIMIT messages within the window, then log this one."""
    log = live_home() / "sent.jsonl"
    now = time.time()
    try:
        rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        rows = []
    recent = [r for r in rows if now - r["at"] < RATE_WINDOW_SECONDS]
    if sum(1 for r in recent if r["from"] == sender and r["to"] == target) >= RATE_LIMIT:
        raise LiveError(f"{sender} already sent {RATE_LIMIT} messages to {target} in the last "
                        f"{RATE_WINDOW_SECONDS // 60} minutes; refusing so two agents cannot relay forever.")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in [*recent, {"from": sender, "to": target, "at": now}]))


def send(target: str, text: str, sender: LiveSession | None, origin: str = "unknown") -> dict:
    session = resolve(target, exclude=sender)
    if sender is not None and session.address == sender.address:
        raise LiveError(f"{session.address} is the sending session itself.")
    sender_label = sender.address if sender else origin
    _check_rate(sender_label, session.address)
    status = ADAPTERS[session.harness].deliver(session, envelope(text, sender, origin))
    return {"to": session.address, "title": session.title, "cwd": session.cwd,
            "delivery": session.delivery, "status": status,
            "from": sender_label}
