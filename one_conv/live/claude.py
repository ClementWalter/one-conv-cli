"""Claude Code: sessions register in `~/.claude/sessions/<pid>.json` and read an inbox socket.

A JSON line `{"type": "user", "message": {...}}` written to the session's
`messagingSocketPath` is queued as a user turn, and starts one when the session is idle.
"""

import json
import os
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


def list_sessions() -> list[LiveSession]:
    sessions = []
    for path in sorted((claude_home() / "sessions").glob("*.json")):
        data = _record(path)
        if not data or not pid_alive(data.get("pid")):
            continue
        socket_path = data.get("messagingSocketPath") or ""
        sessions.append(LiveSession(
            "claude", data["sessionId"], data.get("cwd", ""), data.get("name", ""),
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
