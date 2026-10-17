"""Codex: a loaded thread holds `thread-writer-locks/<id>.lock`; `codex queue` delivers into it.

`codex queue` starts a turn on an idle thread, runs after the current turn on a busy
one, and stores the message for the next resume when no process has the thread loaded.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from . import codex_rpc
from .model import LiveSession, live_home

# The locks directory also holds bookkeeping files such as `.coordination.lock`.
THREAD_ID = codex_rpc.THREAD_ID


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def _names() -> dict[str, str]:
    names = {}
    try:
        lines = (codex_home() / "session_index.jsonl").read_text().splitlines()
    except OSError:
        return names
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("id") and row.get("thread_name"):
            names[row["id"]] = row["thread_name"]
    return names


def _cwd(thread_id: str) -> str:
    for path in (codex_home() / "sessions").rglob(f"rollout-*{thread_id}.jsonl"):
        try:
            with path.open() as handle:
                meta = json.loads(handle.readline())
        except (OSError, ValueError):
            continue
        return meta.get("payload", {}).get("cwd", "")
    return ""


def stopped_threads() -> dict:
    """Threads ended with `live stop`, by stop time. Codex's app-server keeps a closed thread's lock
    for a few minutes; until then a queued message would be recorded but answered by nobody."""
    try:
        rows = json.loads((live_home() / "codex-stopped.json").read_text())
    except (OSError, ValueError):
        return {}
    return {key: at for key, at in rows.items() if isinstance(at, (int, float))} if isinstance(rows, dict) else {}


def mark_stopped(thread_id: str) -> None:
    path = live_home() / "codex-stopped.json"
    rows = stopped_threads()
    rows[thread_id] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(rows))
    os.replace(partial, path)


def ended(thread_id: str, stopped: dict | None = None) -> bool:
    """Whether `live stop` ended this thread and nothing has loaded it since; a resume touches its lock."""
    stopped = stopped_threads() if stopped is None else stopped
    if thread_id not in stopped:
        return False
    try:
        return (codex_home() / "thread-writer-locks" / f"{thread_id}.lock").stat().st_mtime <= stopped[thread_id]
    except OSError:
        return True


def list_sessions() -> list[LiveSession]:
    names, stopped = _names(), stopped_threads()
    sessions = {}
    for lock in sorted((codex_home() / "thread-writer-locks").glob("*.lock")):
        if not THREAD_ID.fullmatch(lock.stem):
            continue
        # A lock not touched since its thread was stopped is the app-server's leftover, not a live session.
        if ended(lock.stem, stopped):
            continue
        sessions[lock.stem] = LiveSession("codex", lock.stem, _cwd(lock.stem), names.get(lock.stem, ""), "loaded",
                                        None, "codex queue")
    # A daemon summary proves runtime activity; unrelated standalone locks remain unverified fallbacks.
    for ident, metadata in codex_rpc.threads(codex_home()).items():
        if ended(ident, stopped):
            continue
        previous = sessions.get(ident)
        sessions[ident] = LiveSession("codex", ident, metadata["cwd"] or (previous.cwd if previous else _cwd(ident)),
                                     (previous.title if previous else names.get(ident, "")) or metadata["title"],
                                     metadata["status"], None, "codex queue")
    return [sessions[ident] for ident in sorted(sessions)]


def stored_session(thread_id: str) -> LiveSession | None:
    """A thread nobody has loaded still accepts queued messages for its next resume."""
    cwd = _cwd(thread_id)
    if not cwd:
        return None
    return LiveSession("codex", thread_id, cwd, _names().get(thread_id, ""), "not loaded", None, "codex queue")


def deliver(session: LiveSession, text: str) -> str:
    result = subprocess.run(["codex", "queue", "--thread", session.id, "--message", text],
                            check=False, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"codex queue exited {result.returncode}")
    return "delivered" if session.status in ("loaded", "idle", "busy") else "queued until the thread is resumed"
