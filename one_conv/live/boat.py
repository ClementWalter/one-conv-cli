"""Boat sandboxes: coding agents in remote sandboxes, reached through the `box` CLI.

An address is `boat:<sandbox>/<conversation>`, using Boat's conversation id; the agent's
native session id inside the sandbox is a different value and is not used for routing.
An idle conversation gets `box prompt --resume`; a busy one gets `box steer`. Boat wraps
a steered text as a message from the user and may stop the running command, so the
one-conv envelope inside it is what still names the real sender.
Replies are read back with `box events`, so nothing in the sandbox has to reach this machine.
"""

import json
import subprocess
import time
import uuid
from pathlib import Path

from .model import LiveSession, live_home, pid_alive

# `box prompt` blocks until the agent's turn ends; this long is spent waiting for Boat to accept it.
ACCEPT_SECONDS = 45


def _box(*args: str, timeout: int = 60) -> list[dict]:
    """Run a read-only `box` command and parse its JSON or JSONL stdout."""
    try:
        result = subprocess.run(["box", *args, "--json", "--no-update"], check=False,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"box {args[0]} gave no answer within {timeout}s; try again later") from error
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


def _operations() -> Path:
    return live_home() / "boat" / "ops"


# What each state means for the sender; only an explicit refusal makes resending safe.
STATE_HELP = {
    "accepted": "Boat accepted the prompt.",
    "pending": "box prompt is still waiting for Boat. Do not resend.",
    "unconfirmed": ("box prompt ended without Boat accepting or refusing, so the prompt may or may "
                    "not have arrived. Do not resend blindly: read the conversation with `one-conv "
                    "live pull`, then run `one-conv live status OP --resolve` to allow another send."),
    "resolved": "Marked as checked; another send to this conversation is allowed.",
}
OP_ID_LENGTH = 12


def operation_state(record: dict) -> str:
    """"accepted", "refused: <reason>", "pending", "unconfirmed", or "resolved" once checked."""
    state = _accepted(Path(record["log"]))
    if state == "accepted":
        return "accepted"
    if state is not None:
        return f"refused: {state}"
    if record.get("resolved"):
        return "resolved"
    return "pending" if pid_alive(record.get("pid")) else "unconfirmed"


def _operation_path(op: str) -> Path:
    if len(op) != OP_ID_LENGTH or any(c not in "0123456789abcdef" for c in op):
        raise RuntimeError(f"no Boat send operation {op!r}")
    return _operations() / f"{op}.json"


def operation(op: str) -> dict:
    try:
        record = json.loads(_operation_path(op).read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"no Boat send operation {op!r}") from error
    state = operation_state(record)
    return {**record, "state": state,
            "help": STATE_HELP.get(state, "Boat refused it; it was not delivered.").replace("OP", op)}


def resolve_operation(op: str) -> dict:
    """Record that an unconfirmed send was checked, so the conversation accepts new sends."""
    record = operation(op)
    if record["state"] != "unconfirmed":
        raise RuntimeError(f"op {op} is {record['state']}; only an unconfirmed send needs resolving")
    stored = json.loads(_operation_path(op).read_text())
    stored["resolved"] = True
    _operation_path(op).write_text(json.dumps(stored))
    return operation(op)


def _pending_for(address: str) -> str | None:
    """An earlier send to `address` whose outcome is still open: pending or unconfirmed."""
    for path in sorted(_operations().glob("*.json")) if _operations().is_dir() else []:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("address") == address and operation_state(record) in ("pending", "unconfirmed"):
            return record["op"]
    return None


def deliver(session: LiveSession, text: str) -> str:
    """Prompt an idle conversation or steer a busy one; the state is read just before, not held.

    A turn that starts between that read and Boat accepting the prompt would receive the
    message mid-turn as a normal prompt; Boat offers no conditional prompt to close this.
    A prompt Boat has not confirmed in time is reported as pending, not failed: the
    detached `box prompt` may still deliver it, so resending could deliver it twice.
    """
    earlier = _pending_for(session.address)
    if earlier:
        raise RuntimeError(f"An earlier message to {session.address} has no confirmed outcome (op {earlier}). "
                           f"Check it with `one-conv live status {earlier}` before sending again.")
    sandbox, conversation = split(session.id)
    if session.status != "idle":
        steered = _steer(sandbox, conversation, text)
        if steered is not None:
            return steered
        # The turn ended between listing and steering: prompt it like an idle conversation.
    op = uuid.uuid4().hex[:OP_ID_LENGTH]
    _operations().mkdir(parents=True, exist_ok=True)
    log_path = _operations() / f"{op}.log.jsonl"
    with log_path.open("w") as log:
        # The text is one argv element: Boat delivers it byte for byte, and it starts with
        # "<one-conv-message", so it cannot be read as an option.
        process = subprocess.Popen(
            ["box", "prompt", sandbox, "--resume", conversation, "--json", "--no-update", text],
            stdout=log, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    record = {"op": op, "address": session.address, "pid": process.pid, "log": str(log_path),
              "started": time.time()}
    (_operations() / f"{op}.json").write_text(json.dumps(record))
    deadline = time.monotonic() + ACCEPT_SECONDS
    while time.monotonic() < deadline:
        state = _accepted(log_path)
        if state == "accepted":
            return f"delivered; the turn runs in the sandbox, read it with `one-conv live pull {session.address}`"
        if state is not None:
            raise RuntimeError(f"Boat refused the prompt: {state}")
        if process.poll() is not None:
            # A local exit is not a refusal from Boat: the prompt may have been forwarded anyway.
            return (f"unconfirmed: box prompt exited {process.returncode} without Boat accepting or "
                    f"refusing (op {op}). Do not resend; check it with `one-conv live status {op}`")
        time.sleep(0.2)
    return (f"pending: Boat has not confirmed the prompt yet (op {op}). Do not resend; "
            f"check it with `one-conv live status {op}`")


def _steer(sandbox: str, conversation: str, text: str) -> str | None:
    """Add `text` to the running turn; None when the turn has already ended."""
    try:
        result = subprocess.run(
            ["box", "steer", sandbox, "--convo", conversation, "--json", "--no-update", text],
            check=False, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Boat gave no answer to the steer within 60s; it may still arrive. "
                           f"Do not resend; read the conversation with `one-conv live pull boat:{sandbox}/{conversation}`"
                           ) from error
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("status") == "steered":
            return ("steered into the running turn (Boat shows it as a message from the user; the "
                    f"envelope names the sender); read the reply with `one-conv live pull boat:{sandbox}/{conversation}`")
        if row.get("code") == "no_running_turn":
            return None
        if row.get("event") == "error":
            raise RuntimeError(f"Boat refused the steer: {row.get('code')}: {row.get('error', '')}")
    raise RuntimeError(f"box steer exited {result.returncode} without an answer; it may still arrive. "
                       f"Do not resend; read the conversation with `one-conv live pull boat:{sandbox}/{conversation}`")


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
