"""Antigravity CLI (agy): lifecycle hooks register each conversation and hand it queued messages.

agy runs its agent inside a local language server that only answers requests carrying a CSRF
token it never writes to disk, and no hook or MCP server is told it. So delivery has two paths:

* every session: the `SessionStart`, `PreInvocation` and `Stop` hooks record the conversation
  and drain `<live>/inbox/agy/<id>/`, injecting messages as a user turn before the next model
  call, or continuing the loop when a turn ends. An idle session reads them on its next turn.
* a session started by `one-conv live agy`: it runs with a token one-conv chose, so a message
  goes straight in through the server's `SendUserCascadeMessage`, idle or busy.

This module is stdlib-only: the hook runs it on every model call, where a uv start costs too much.
"""

import json
import os
import shlex
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .model import LiveSession, is_session_id, live_home, pid_alive

# Read by the hook from agy's environment: set only by `one-conv live agy`, which passes the same
# value as agy's hidden `--csrf_token` flag.
TOKEN_ENV = "ONE_CONV_AGY_CSRF"
HOOK_NAME = "one-conv-live"
CASCADE_SEND = "/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage"
CONVERSATION_GET = "/jetski.product.v1.ConversationService/GetConversation"


def agy_home() -> Path:
    return Path(os.environ.get("ONE_CONV_AGY_HOME", str(Path.home() / ".gemini" / "antigravity-cli")))


def config_dir() -> Path:
    """agy's global customization root, where `hooks.json` and `mcp_config.json` live."""
    return Path(os.environ.get("ONE_CONV_AGY_CONFIG", str(Path.home() / ".gemini" / "config")))


def _registry() -> Path:
    return live_home() / "agy"


def inbox(conversation_id: str) -> Path:
    if not is_session_id(conversation_id):
        raise ValueError(f"not an agy conversation id: {conversation_id!r}")
    return live_home() / "inbox" / "agy" / conversation_id


def title(conversation_id: str) -> str:
    """The title agy keeps in its conversation index, else its summary of the first prompt."""
    database = agy_home() / "conversation_summaries.db"
    if not database.is_file():
        return ""
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1) as conn:
            row = conn.execute("SELECT title, preview FROM conversation_summaries WHERE conversation_id = ?",
                               (conversation_id,)).fetchone()
    except sqlite3.Error:
        return ""
    return next((value.strip() for value in (row or ()) if isinstance(value, str) and value.strip()), "")


def _records() -> list[dict]:
    rows = []
    for path in sorted(_registry().glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and is_session_id(str(data.get("conversationId") or "")):
            rows.append(data)
    return rows


def _session(data: dict) -> LiveSession:
    conversation_id = data["conversationId"]
    delivery = "language server" if data.get("lsAddress") and data.get("csrfToken") else "lifecycle hook"
    return LiveSession("agy", conversation_id, data.get("cwd", ""), title(conversation_id),
                       data.get("status", ""), data.get("pid"), delivery, str(inbox(conversation_id)))


def list_sessions() -> list[LiveSession]:
    return [_session(data) for data in _records() if pid_alive(data.get("pid"))]


def session_for_pid(pid: int) -> LiveSession | None:
    return next((_session(data) for data in _records() if data.get("pid") == pid), None)


def _record(conversation_id: str) -> dict | None:
    return next((data for data in _records() if data["conversationId"] == conversation_id), None)


def _post(address: str, token: str, route: str, body: dict, timeout: float = 10) -> dict:
    request = urllib.request.Request(f"http://{address}{route}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                                              "x-antigravity-csrf-token": token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def deliver(session: LiveSession, text: str) -> str:
    if not text:
        # An empty user message still starts a turn, in which the agent asks what to do.
        raise ValueError("an agy message needs text")
    record = _record(session.id) or {}
    if record.get("lsAddress") and record.get("csrfToken") and pid_alive(record.get("pid")):
        try:
            _post(record["lsAddress"], record["csrfToken"], CASCADE_SEND,
                  {"cascadeId": session.id, "items": [{"text": text}]})
            return "delivered"
        except (OSError, ValueError, urllib.error.URLError):
            pass  # The server restarted or moved; the hooks still drain the inbox.
    _queue(session.id, text)
    return "queued until this session's next model call or turn end"


def _queue(conversation_id: str, text: str) -> Path:
    target = inbox(conversation_id)
    target.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex}.json"
    # The hook only picks up `*.json`, so the rename publishes a complete file.
    partial = target / f".{name}.tmp"
    partial.write_text(json.dumps({"text": text}))
    partial.rename(target / name)
    return target / name


def _drain(conversation_id: str) -> list[str]:
    texts = []
    directory = inbox(conversation_id)
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        claimed = path.with_name(path.name + f".{os.getpid()}.claimed")
        try:
            path.rename(claimed)
        except OSError:
            continue  # Another hook run claimed it first.
        try:
            text = json.loads(claimed.read_text()).get("text")
        except (OSError, ValueError, AttributeError):
            text = None
        # A malformed file is dropped so it cannot block or lose the messages around it.
        claimed.unlink(missing_ok=True)
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _listening_ports(pid: int) -> list[int]:
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-P", "-Fn"],
                                check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line.rsplit(":", 1)[1]) for line in result.stdout.splitlines()
            if line.startswith("n") and line.rsplit(":", 1)[-1].isdigit()]


def find_server(pid: int, token: str, conversation_id: str) -> str | None:
    """The `host:port` of agy's plain-HTTP server; its other port speaks only HTTPS."""
    for port in _listening_ports(pid):
        address = f"localhost:{port}"
        try:
            _post(address, token, CONVERSATION_GET, {"conversationId": conversation_id}, timeout=2)
        except (OSError, ValueError, urllib.error.URLError):
            continue
        return address
    return None


def _write_record(record: dict) -> None:
    _registry().mkdir(parents=True, exist_ok=True)
    target = _registry() / f"{record['conversationId']}.json"
    partial = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(record))
    partial.rename(target)


def register(payload: dict, pid: int, token: str | None) -> str | None:
    """Record the conversation as running in agy process `pid`; returns its id."""
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str) or not is_session_id(conversation_id):
        return None
    previous = _record(conversation_id) or {}
    # One agy process shows one conversation; a `/resume` or new chat moves it.
    for data in _records():
        if data.get("pid") == pid and data["conversationId"] != conversation_id:
            (_registry() / f"{data['conversationId']}.json").unlink(missing_ok=True)
    roots = payload.get("workspacePaths") or []
    record = {"conversationId": conversation_id, "pid": pid, "cwd": payload.get("cwd") or (roots[0] if roots else ""),
              "status": "idle" if payload.get("hookEventName") == "Stop" else "busy", "updatedAt": time.time()}
    if token:
        same_server = previous.get("pid") == pid and previous.get("csrfToken") == token and previous.get("lsAddress")
        address = previous["lsAddress"] if same_server else find_server(pid, token, conversation_id)
        if address:
            record.update(lsAddress=address, csrfToken=token)
    _write_record(record)
    return conversation_id


def run_hook(payload: dict, pid: int, token: str | None = None) -> dict:
    """One hook event: register the conversation, then hand over what its inbox holds.

    Before a model call the messages become user turns; when the loop stops they keep it going.
    """
    conversation_id = register(payload, pid, token)
    if conversation_id is None:
        return {}
    texts = _drain(conversation_id)
    if not texts:
        return {}
    event = payload.get("hookEventName")
    if event == "Stop":
        return {"decision": "continue", "reason": "\n\n".join(texts)}
    return {"injectSteps": [{"userMessage": text} for text in texts]}


def launch_command(args: list[str]) -> tuple[list[str], dict]:
    """argv and environment for an agy whose language server takes a token one-conv knows."""
    token = str(uuid.uuid4())
    return ["agy", f"--csrf_token={token}", *args], {**os.environ, TOKEN_ENV: token}


def hook_command(python: str, script: Path) -> str:
    # `exec` keeps agy the hook's parent, which is how the hook learns agy's pid.
    return f"exec {shlex.quote(python)} -I {shlex.quote(str(script))}"


def hook_events(command: str) -> dict:
    handler = [{"type": "command", "command": command, "timeout": 30}]
    return {"SessionStart": handler, "PreInvocation": handler, "Stop": handler}


def main() -> None:
    import sys
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        result = run_hook(payload if isinstance(payload, dict) else {}, os.getppid(), os.environ.get(TOKEN_ENV))
    except OSError:
        result = {}  # A hook failure must never stop the agent.
    sys.stdout.write(json.dumps(result))
