"""A Claude Code question (AskUserQuestion) answered from the workspace.

Claude Code runs a PreToolUse hook before showing the question in the terminal. The hook posts the
question to the human as a direct message from the asking session, marked so the web app offers
its options as buttons, then waits. While a question waits, the bridge files the human's next
message to that session as the answer instead of typing it into the terminal. The hook answers the
tool with it (`updatedInput.answers`), so the session carries on as if it had been picked there.
A question still unanswered when the wait ends is declined with a note telling the agent the answer
will come as a message, so a late answer from the workspace is never typed behind a terminal dialog.
A question that could not be posted is left to the terminal. A pending question is only answered
while its hook process lives: one killed mid-wait (an interrupt) lets later messages through.
"""

import json
import logging
import os
import signal
import threading
import time
import uuid
from pathlib import Path

from .model import live_home, pid_alive

logger = logging.getLogger(__name__)
HUMAN = "workspace/user"
# The line the web app reads to offer the options as buttons; other readers see a plain comment.
MARKER = "<!-- one-conv-ask "
# How long a question waits for the workspace before the terminal asks instead.
WAIT_SECONDS = float(os.environ.get("ONE_CONV_ASK_SECONDS", "1800"))
POLL_SECONDS = 1.0
# What the agent reads when the wait ends unanswered: the answer can still arrive, as a message.
UNANSWERED = ("The user has not answered this question yet. It stays open in their workspace, and their answer "
              "will reach you later as a one-conv message. Do not ask it again: end your turn, or carry on only "
              "with work that does not depend on the answer.")


def asks_dir() -> Path:
    return live_home() / "asks"


def _pending(session_id: str) -> Path:
    return asks_dir() / f"{session_id}.json"


def _answer(session_id: str) -> Path:
    return asks_dir() / f"{session_id}.answer.json"


def question_text(questions: list[dict]) -> str:
    """The message the human reads: each question with its options, then the marker for the buttons."""
    lines = ["**A question for you** (tap an option, or reply with your own answer)"]
    for item in questions:
        lines.append("")
        lines.append(f"**{item.get('header') or 'Question'}**: {item.get('question', '')}")
        for option in item.get("options") or []:
            description = option.get("description")
            lines.append(f"- {option.get('label', '')}" + (f": {description}" if description else ""))
        if item.get("multiSelect"):
            lines.append("_Several answers allowed._")
    marker = [{"question": item.get("question", ""), "header": item.get("header") or "",
               "options": [{"label": option.get("label", ""), "description": option.get("description") or ""}
                           for option in item.get("options") or []],
               "multiSelect": bool(item.get("multiSelect"))} for item in questions]
    lines.append("")
    lines.append(MARKER + json.dumps({"questions": marker}) + " -->")
    return "\n".join(lines)


def _choice(item: dict, text: str) -> str:
    """The option labels a reply names, matched without case; any other reply is a free answer."""
    labels = {option.get("label", "").casefold(): option.get("label", "") for option in item.get("options") or []}
    parts = [part.strip() for part in text.split(",")] if item.get("multiSelect") else [text.strip()]
    if all(part.casefold() in labels for part in parts if part):
        return ", ".join(labels[part.casefold()] for part in parts if part)
    return text.strip()


def answers(questions: list[dict], reply: str) -> dict[str, str]:
    """Map each question to its answer: `Header: answer` lines, else the whole reply for every question."""
    by_header = {}
    for line in reply.splitlines():
        header, sep, value = line.partition(":")
        if sep and header.strip():
            by_header[header.strip().casefold()] = value.strip()
    result = {}
    for item in questions:
        value = by_header.get((item.get("header") or "").strip().casefold()) if len(questions) > 1 else None
        result[item.get("question", "")] = _choice(item, value if value is not None else reply)
    return result


def file_answer(session_id: str, sender_route: str, text: str) -> bool:
    """The bridge's side: file a human message as the answer when this session has a question waiting."""
    if sender_route != HUMAN or not _waiting(session_id):
        return False
    _answer(session_id).write_text(json.dumps({"text": text, "at": time.time()}))
    return True


def _waiting(session_id: str) -> bool:
    """A question is pending and its hook still runs; a pending file left by a killed hook is removed."""
    try:
        pending = json.loads(_pending(session_id).read_text())
    except FileNotFoundError:
        return False
    except ValueError:
        pending = {}
    if pid_alive(pending.get("pid")):
        return True
    _pending(session_id).unlink(missing_ok=True)
    return False


def _post(session_id: str, text: str) -> bool:
    """Send the question to the human from the asking session, through this machine's bridge."""
    from . import claude, hubclient, router
    session = next((s for s in claude.list_sessions() if s.id == session_id), None)
    if session is None:
        return False
    try:
        router.send(HUMAN, text, session, reply="expected")
    except (router.LiveError, hubclient.HubRefused, hubclient.HubUnavailable, OSError) as error:
        # A question the workspace never got is left to the terminal.
        logger.warning("could not post the question to the workspace: %s", error)
        return False
    return True


def run_hook(payload: dict, wait: float = WAIT_SECONDS) -> dict:
    """PreToolUse hook for AskUserQuestion: the workspace's answer, a decline once the wait ends unanswered,
    or {} to let the terminal ask when the question could not be posted."""
    tool_input = payload.get("tool_input") or {}
    questions, session_id = tool_input.get("questions"), payload.get("session_id")
    if payload.get("tool_name") != "AskUserQuestion" or not questions or not session_id or tool_input.get("answers"):
        return {}
    asks_dir().mkdir(parents=True, exist_ok=True)
    _answer(session_id).unlink(missing_ok=True)
    _pending(session_id).write_text(json.dumps({"id": uuid.uuid4().hex, "pid": os.getpid(), "questions": questions}))
    # Claude Code ends the hook with SIGTERM when the turn is interrupted; exiting runs the cleanup below.
    previous = signal.signal(signal.SIGTERM, _exit) if threading.current_thread() is threading.main_thread() else None
    parent = os.getppid()
    try:
        if not _post(session_id, question_text(questions)):
            return {}
        deadline = time.monotonic() + wait
        # A hook orphaned by its runner has nobody to answer to.
        while time.monotonic() < deadline and os.getppid() == parent:
            if _answer(session_id).exists():
                reply = json.loads(_answer(session_id).read_text())["text"]
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                               "updatedInput": {"questions": questions,
                                                                "answers": answers(questions, reply)}}}
            time.sleep(POLL_SECONDS)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": UNANSWERED}}
    finally:
        _pending(session_id).unlink(missing_ok=True)
        _answer(session_id).unlink(missing_ok=True)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _exit(*_):
    raise SystemExit(0)
