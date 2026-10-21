"""Cursor: the `stop` hook registers each chat and hands it queued messages as a follow-up.

A chat only picks a message up when one of its turns ends, so delivery into an idle chat
waits for the user's next prompt. When Cursor's Desktop Bridge is enabled (a discovery
file under `~/.cursor/desktop-bridge/`), `cursor desktop send` delivers immediately instead.
"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from .model import LiveSession, is_session_id, live_home

# Chats whose last turn ended longer ago than this are treated as closed.
RECENT_SECONDS = 12 * 3600


def cursor_home() -> Path:
    return Path(os.environ.get("ONE_CONV_CURSOR_HOME", str(Path.home() / ".cursor")))


def _registry() -> Path:
    return live_home() / "cursor"


def inbox(conversation_id: str) -> Path:
    if not is_session_id(conversation_id):
        raise ValueError(f"not a Cursor conversation id: {conversation_id!r}")
    return live_home() / "inbox" / "cursor" / conversation_id


def _bridge_enabled() -> bool:
    return any((cursor_home() / "desktop-bridge").glob("*.json"))


def _bridge_threads() -> list[LiveSession]:
    result = subprocess.run(["cursor", "desktop", "ls", "--json"], check=False, capture_output=True, text=True, timeout=30)
    try:
        rows = json.loads(result.stdout)
    except ValueError:
        return []
    rows = rows.get("threads", rows) if isinstance(rows, dict) else rows
    return [LiveSession("cursor", row["id"], "", row.get("title", ""), row.get("status", ""), None,
                        "desktop bridge")
            for row in rows if isinstance(row, dict) and row.get("id") and row.get("source") != "draft"]


def _hook_chats() -> list[LiveSession]:
    sessions = []
    now = time.time()
    for path in sorted(_registry().glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if now - data.get("updatedAt", 0) > RECENT_SECONDS:
            continue
        sessions.append(LiveSession("cursor", data["conversationId"], data.get("cwd", ""), "",
                                    "turn ended", None, "stop hook", str(inbox(data["conversationId"]))))
    return sessions


def list_sessions() -> list[LiveSession]:
    if _bridge_enabled():
        bridged = _bridge_threads()
        if bridged:
            return bridged
    return _hook_chats()


def deliver(session: LiveSession, text: str) -> str:
    if session.delivery == "desktop bridge":
        result = subprocess.run(["cursor", "desktop", "send", session.id, "--stdin", "--json"],
                                input=text, check=False, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"cursor desktop send exited {result.returncode}")
        return result.stdout.strip() or "delivered"
    from . import spawn  # spawn imports this module
    # An idle CLI chat runs no hook, so the inbox would wait; a spawned one's pane takes the text now.
    if pane := spawn.pane_for(session.address):
        spawn.type_into(pane, text)
        return f"typed into tmux session {pane}"
    target = inbox(session.id)
    target.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex}.json"
    partial = target / f".{name}.tmp"
    partial.write_text(json.dumps({"text": text}))
    partial.rename(target / name)
    return "queued until this chat's current turn ends"


def chat_for_pid(pid: int) -> str | None:
    """A CLI chat keeps `chats/<workspace hash>/<chat id>/store.db` open; the id is the hook's conversation_id."""
    result = subprocess.run(["lsof", "-a", "-p", str(pid), "-Fn"], check=False, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        path = Path(line[1:])
        if line.startswith("n") and path.name == "store.db" and path.parent.parent.parent.name == "chats":
            return path.parent.name
    return None


def run_stop_hook(payload: dict) -> dict:
    """Record the chat as live and return its queued messages as one follow-up turn."""
    conversation_id = payload.get("conversation_id")
    if not isinstance(conversation_id, str) or not is_session_id(conversation_id):
        return {}
    roots = payload.get("workspace_roots") or []
    _registry().mkdir(parents=True, exist_ok=True)
    record = {"conversationId": conversation_id, "cwd": roots[0] if roots else "", "updatedAt": time.time()}
    partial = _registry() / f".{conversation_id}.tmp"
    partial.write_text(json.dumps(record))
    partial.rename(_registry() / f"{conversation_id}.json")
    texts = []
    directory = inbox(conversation_id)
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        claimed = path.with_name(path.name + ".claimed")
        try:
            path.rename(claimed)
        except OSError:
            continue
        try:
            text = json.loads(claimed.read_text()).get("text")
        except (OSError, ValueError, AttributeError):
            text = None
        # A malformed file is dropped so it cannot block or lose the messages around it.
        claimed.unlink(missing_ok=True)
        if isinstance(text, str) and text:
            texts.append(text)
    return {"followup_message": "\n\n".join(texts)} if texts else {}
