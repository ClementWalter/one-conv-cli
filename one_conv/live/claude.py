"""Claude Code: sessions register in `~/.claude/sessions/<pid>.json` and read an inbox socket.

A JSON line `{"type": "user", "message": {...}}` written to the session's
`messagingSocketPath` is queued as a user turn, and starts one when the session is idle.
"""

import json
import os
import re
import socket
import uuid
from pathlib import Path

from .model import LiveSession, pid_alive


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def _record(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("sessionId") else None


# Per transcript: bytes already scanned, and the latest /rename and generated titles seen in them.
_titles: dict[Path, tuple[int, str, str]] = {}


def transcript(session_id: str, cwd: str) -> Path:
    """Claude Code names a project folder after its cwd with every other character turned into a dash."""
    return claude_home() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"


def session_title(session_id: str, cwd: str) -> str:
    """The name Claude Code shows for a session: its /rename title, else the title it generated.

    Transcripts grow to megabytes and are read at every publication, so only the bytes
    appended since the last read are scanned.
    """
    path = transcript(session_id, cwd)
    offset, custom, generated = _titles.get(path, (0, "", ""))
    try:
        with path.open("rb") as handle:
            if handle.seek(0, os.SEEK_END) < offset:
                offset, custom, generated = 0, "", ""
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return ""
    # A line still being written waits for the next read.
    complete = chunk[:chunk.rfind(b"\n") + 1]
    for line in complete.splitlines():
        if b'"custom-title"' not in line and b'"ai-title"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "custom-title" and isinstance(entry.get("customTitle"), str):
            custom = entry["customTitle"].strip()
        elif entry.get("type") == "ai-title" and isinstance(entry.get("aiTitle"), str):
            generated = entry["aiTitle"].strip()
    _titles[path] = (offset + len(complete), custom, generated)
    return custom or generated


def list_sessions() -> list[LiveSession]:
    """One entry per session id: two processes resuming the same session are one address, the latest active."""
    records: dict[str, dict] = {}
    for path in sorted((claude_home() / "sessions").glob("*.json")):
        data = _record(path)
        if not data or not pid_alive(data.get("pid")):
            continue
        kept = records.get(data["sessionId"])
        if kept is None or (data.get("updatedAt") or 0) > (kept.get("updatedAt") or 0):
            records[data["sessionId"]] = data
    sessions = []
    for data in records.values():
        socket_path = data.get("messagingSocketPath") or ""
        sessions.append(LiveSession(
            "claude", data["sessionId"], data.get("cwd", ""),
            session_title(data["sessionId"], data.get("cwd", "")) or data.get("name", ""),
            data.get("status", ""), data.get("pid"),
            "socket" if socket_path and Path(socket_path).exists() else "none", socket_path))
    return sessions


def session_for_pid(pid: int) -> LiveSession | None:
    data = _record(claude_home() / "sessions" / f"{pid}.json")
    if not data:
        return None
    return LiveSession("claude", data["sessionId"], data.get("cwd", ""), data.get("name", ""),
                       data.get("status", ""), pid, "socket", data.get("messagingSocketPath") or "")


def deliver(session: LiveSession, text: str) -> str:
    if not session.endpoint:
        raise RuntimeError(f"{session.address} has no messaging socket (Claude Code too old?)")
    frame = {"type": "user", "msg_id": uuid.uuid4().hex, "message": {"role": "user", "content": text}}
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(session.endpoint)
        connection.sendall((json.dumps(frame) + "\n").encode())
    return "delivered"
