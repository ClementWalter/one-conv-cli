"""Boat sandboxes: coding agents in remote sandboxes, reached through the `box` CLI.

An address is `boat:<sandbox>/<conversation>`, using Boat's conversation id; the agent's
native session id inside the sandbox is a different value and is not used for routing.
Messages go only into idle conversations with `box prompt --resume`: Boat's `steer`
relabels the text as the user's own instruction, which a peer message must never become.
Replies are read back with `box events`, so nothing in the sandbox has to reach this machine.
"""

import json
import subprocess
import time
import uuid

from .model import LiveSession, live_home

# `box prompt` blocks until the agent's turn ends; this long is spent waiting for Boat to accept it.
ACCEPT_SECONDS = 45


def _box(*args: str, timeout: int = 60) -> list[dict]:
    """Run a read-only `box` command and parse its JSON or JSONL stdout."""
    result = subprocess.run(["box", *args, "--json", "--no-update"], check=False,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"box {args[0]} failed: {result.stderr.strip()[-300:]}")
    rows = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(("{", "[")):
            rows.append(json.loads(line))
    return rows


def split(ident: str) -> tuple[str, str]:
    sandbox, _, conversation = ident.partition("/")
    if not sandbox or not conversation:
        raise ValueError(f"a Boat address is boat:<sandbox>/<conversation>, got boat:{ident}")
    return sandbox, conversation


def list_sessions() -> list[LiveSession]:
    sessions = []
    for page in _box("list"):
        for sandbox in page.get("sandboxes", []):
            for row in _box("conversations", sandbox["id"]):
                data = row.get("data") or {}
                if row.get("event") != "conversation" or not data.get("id"):
                    continue
                sessions.append(LiveSession(
                    "boat", f"{sandbox['id']}/{data['id']}", sandbox.get("name", ""),
                    (data.get("lastPromptPreview") or "")[:80],
                    "busy" if data.get("running") else "idle", None, "box prompt",
                    data.get("lastHarness") or ""))
    return sessions


def _accepted(log_path) -> str | None:
    """"accepted" once Boat echoes the prompt, an error text if it refused, None while waiting."""
    try:
        lines = log_path.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "error":
            return f"{row.get('code', 'error')}: {row.get('error', '')}"
        if row.get("event") == "chat" and (row.get("data") or {}).get("type") == "prompt":
            return "accepted"
    return None


def deliver(session: LiveSession, text: str) -> str:
    """Prompt an idle conversation; the idle state is read just before, not held.

    A turn that starts between that read and Boat accepting the prompt would receive the
    message mid-turn as a normal prompt; Boat offers no conditional prompt to close this.
    """
    if session.status != "idle":
        raise RuntimeError(f"{session.address} is running a turn. Boat can only steer a busy "
                           "conversation, which would present this message as the user's own "
                           "instruction; send it again once the turn has ended.")
    sandbox, conversation = split(session.id)
    logs = live_home() / "boat"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{conversation}-{uuid.uuid4().hex[:8]}.jsonl"
    with log_path.open("w") as log:
        # The text is one argv element: Boat delivers it byte for byte, and it starts with
        # "<one-conv-message", so it cannot be read as an option.
        process = subprocess.Popen(
            ["box", "prompt", sandbox, "--resume", conversation, "--json", "--no-update", text],
            stdout=log, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + ACCEPT_SECONDS
    while time.monotonic() < deadline:
        state = _accepted(log_path)
        if state == "accepted":
            return f"delivered; the turn runs in the sandbox, read it with `one-conv live pull {session.address}`"
        if state is not None:
            raise RuntimeError(f"Boat refused the prompt: {state}")
        if process.poll() is not None:
            raise RuntimeError(f"box prompt exited {process.returncode} before accepting; see {log_path}")
        time.sleep(0.5)
    raise RuntimeError(f"Boat did not confirm the prompt within {ACCEPT_SECONDS}s; see {log_path}")


def pull(ident: str) -> list[dict]:
    """The conversation's finished prompts and responses, oldest first."""
    sandbox, conversation = split(ident)
    turns = []
    for row in _box("events", sandbox, "--convo", conversation, timeout=120):
        data = row.get("data") or {}
        inner = data.get("data") or {}
        if row.get("event") != "chat" or row.get("final") is False or inner.get("is_reverted"):
            continue
        role, text = {"prompt": ("user", inner.get("prompt")),
                      "response": ("assistant", inner.get("content"))}.get(data.get("type"), (None, None))
        # Tool-only steps arrive as responses with no text; they carry nothing to read.
        if role and isinstance(text, str) and text.strip():
            turns.append({"ts": data.get("timestamp"), "role": role, "text": text})
    return sorted(turns, key=lambda turn: turn["ts"] or 0)
