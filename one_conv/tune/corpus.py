"""Load exported session documents as plain role/text conversations.

The export format (`one-conv local export`) is the single input of the tuning
pipeline, so every plugged source reaches training through one normalized,
secret-scrubbed shape.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Agentic coding tools: the only sources whose replies become training targets.
CODING_SOURCES = ("claude", "codex", "cursor", "cursor-cli", "omp", "codex-cloud")
AGENT_SOURCES = ("claude", "codex", "cursor", "cursor-cli", "omp", "chatgpt",
                 "claude-chat", "codex-cloud", "cowork-cloud")


@dataclass(frozen=True)
class Message:
    role: str
    text: str


@dataclass(frozen=True)
class Session:
    source: str
    session: str
    cwd: str
    author: str
    mtime: float
    messages: tuple[Message, ...]

    @property
    def key(self) -> str:
        return f"{self.source}:{self.session}"


# Injected by harnesses, relays or schedulers rather than typed by the user.
AUTOMATED_PREFIXES = (
    "Tu es le même cerveau que sur tous les autres canaux",
    "Tu réponds à Clément en direct",
    "Another Claude session sent a message:",
    "Warning: apply_patch was requested via exec_command",
    "<local-command-caveat>",
    "<system_notification>",
    "<task-notification>",
    "This session is being continued from a previous conversation",
    "Base directory for this skill:",
    "Run the `continual-learning` skill now",
    "<user_shell_command>",
)


def is_automated(text: str) -> bool:
    return text.lstrip().startswith(AUTOMATED_PREFIXES)


def spoken_text(text: str) -> str:
    """ChatGPT voice turns are stored as JSON parts; keep only what was said or typed."""
    stripped = text.lstrip()
    if not stripped.startswith('[{"content_type"'):
        return text
    try:
        parts = json.loads(stripped)
    except ValueError:
        return text
    spoken = []
    for part in parts if isinstance(parts, list) else []:
        for item in part.get("parts", []) if isinstance(part, dict) else []:
            if isinstance(item, str):
                spoken.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                spoken.append(item["text"])
    return " ".join(chunk.strip() for chunk in spoken if chunk.strip()) or text


def parse_session(doc: object) -> Session | None:
    """Return None for documents that are not exported conversations."""
    if not isinstance(doc, dict) or not isinstance(doc.get("turns"), list):
        return None
    if not isinstance(doc.get("source"), str) or not isinstance(doc.get("session"), str):
        return None
    messages = []
    for turn in doc["turns"]:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            continue
        if not isinstance(turn.get("text"), str) or not turn["text"].strip():
            continue
        text = spoken_text(turn["text"])
        if turn["role"] == "user" and is_automated(text):
            continue
        messages.append(Message(turn["role"], text))
    messages = tuple(messages)
    return Session(doc["source"], doc["session"], str(doc.get("cwd") or ""),
                   str(doc.get("author") or "human"), float(doc.get("mtime") or 0.0), messages)


def load_sessions(roots: list[Path], sources: tuple[str, ...] = AGENT_SOURCES,
                  include_system: bool = False) -> list[Session]:
    """Read every export under `roots`, keeping the newest copy of each session."""
    newest: dict[str, Session] = {}
    for root in roots:
        for path in sorted(root.expanduser().rglob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                session = parse_session(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.debug("Skipping unreadable export %s", path)
                continue
            if session is None or session.source not in sources:
                continue
            # Heartbeat and worker sessions carry no human acceptance signal.
            if session.author != "human" and not include_system:
                continue
            current = newest.get(session.key)
            if current is None or session.mtime > current.mtime:
                newest[session.key] = session
    # The same conversation can also be stored under two ids (old and new caches, another machine's export).
    by_content: dict[int, Session] = {}
    for session in newest.values():
        content = hash(session.messages)
        if content not in by_content or session.mtime > by_content[content].mtime:
            by_content[content] = session
    return sorted(by_content.values(), key=lambda s: s.mtime)


def merge_runs(messages: tuple[Message, ...]) -> list[Message]:
    """Join consecutive same-role messages; exports split one agent turn per tool call."""
    merged: list[Message] = []
    for message in messages:
        if merged and merged[-1].role == message.role:
            merged[-1] = Message(message.role, merged[-1].text + "\n\n" + message.text)
        else:
            merged.append(message)
    return merged
