"""Codex: a loaded thread holds `thread-writer-locks/<id>.lock`; `codex queue` delivers into it.

`codex queue` starts a turn on an idle thread, runs after the current turn on a busy
one, and stores the message for the next resume when no process has the thread loaded.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from .model import LiveSession

# The locks directory also holds bookkeeping files such as `.coordination.lock`.
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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


def list_sessions() -> list[LiveSession]:
    names = _names()
    return [LiveSession("codex", lock.stem, _cwd(lock.stem), names.get(lock.stem, ""), "loaded",
                        None, "codex queue")
            for lock in sorted((codex_home() / "thread-writer-locks").glob("*.lock"))
            if THREAD_ID.fullmatch(lock.stem)]


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
    return "delivered" if session.status == "loaded" else "queued until the thread is resumed"
