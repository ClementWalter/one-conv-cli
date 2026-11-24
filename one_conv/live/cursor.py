"""Cursor: the `stop` hook registers each chat and hands it queued messages as a follow-up.

A running `agent` CLI chat is listed from its process before its first turn ends.

A chat only picks a message up when one of its turns ends, so delivery into an idle chat
waits for the user's next prompt. When Cursor's Desktop Bridge is
enabled (a discovery file under `~/.cursor/desktop-bridge/`), `cursor desktop send` delivers
immediately instead.
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
                        "desktop bridge", product="cursor-ide")
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
        sessions.append(LiveSession("cursor", data["conversationId"], data.get("cwd", ""), title(data["conversationId"]),
                                    "turn ended", None, "stop hook", str(inbox(data["conversationId"])),
                                    product=product(data["conversationId"])))
    return sessions


def title(conversation_id: str) -> str:
    """The title the `agent` CLI stored in the chat's meta.json; the IDE's chats have none here."""
    for path in (cursor_home() / "chats").glob(f"*/{conversation_id}/meta.json"):
        try:
            value = json.loads(path.read_text()).get("title")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def product(conversation_id: str) -> str:
    """The `agent` CLI keeps each chat under `~/.cursor/chats/<workspace hash>/<id>`; the IDE keeps its own elsewhere."""
    return "cursor-cli" if any((cursor_home() / "chats").glob(f"*/{conversation_id}")) else "cursor-ide"


def list_sessions() -> list[LiveSession]:
    if _bridge_enabled():
        bridged = _bridge_threads()
        if bridged:
            return bridged
    running = {session.id: session for session in _running_chats()}
    # A chat registers through its stop hook only once a turn ends; its process shows it before that.
    return [*running.values(), *(session for session in _hook_chats() if session.id not in running)]


def is_cursor_agent(command: str) -> bool:
    """The CLI binary is `cursor-agent`, usually started through the `agent` symlink it installs."""
    name = Path(command).name
    if name == "cursor-agent":
        return True
    return name == "agent" and os.path.isabs(command) and Path(command).resolve().name == "cursor-agent"


def _agent_pids() -> list[int]:
    result = subprocess.run(["ps", "-Ao", "pid=,comm="], check=False, capture_output=True, text=True, timeout=30)
    pids = []
    for line in result.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and is_cursor_agent(command.strip()):
            pids.append(int(pid))
    return pids


def _running_chats() -> list[LiveSession]:
    sessions = []
    for pid in _agent_pids():
        chat, cwd = _open_chat(pid)
        if chat and is_session_id(chat):
            sessions.append(LiveSession("cursor", chat, cwd, title(chat), "running", pid, "stop hook",
                                        str(inbox(chat)), product="cursor-cli"))
    return sessions


def deliver(session: LiveSession, text: str) -> str:
    if session.delivery == "desktop bridge":
        result = subprocess.run(["cursor", "desktop", "send", session.id, "--stdin", "--json"],
                                input=text, check=False, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"cursor desktop send exited {result.returncode}")
        return result.stdout.strip() or "delivered"
    _queue(session.id, text)
    return "queued until this chat's current turn ends"


def _queue(conversation_id: str, text: str) -> Path:
    target = inbox(conversation_id)
    target.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex}.json"
    partial = target / f".{name}.tmp"
    partial.write_text(json.dumps({"text": text}))
    partial.rename(target / name)
    return target / name


def chat_for_pid(pid: int) -> str | None:
    return _open_chat(pid)[0]


def _open_chat(pid: int) -> tuple[str | None, str]:
    """(chat id, working directory) of an `agent` process.

    A CLI chat keeps `chats/<workspace hash>/<chat id>/store.db` open; the id is the hook's conversation_id.
    """
    result = subprocess.run(["lsof", "-p", str(pid), "-Ffn"], check=False, capture_output=True, text=True, timeout=30)
    chat, cwd, descriptor = None, "", ""
    for line in result.stdout.splitlines():
        if line.startswith("f"):
            descriptor = line[1:]
        elif line.startswith("n"):
            path = Path(line[1:])
            if descriptor == "cwd":
                cwd = str(path)
            elif chat is None and path.name == "store.db" and path.parent.parent.parent.name == "chats":
                chat = path.parent.name
    return chat, cwd


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
