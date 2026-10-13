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


def parse_session(doc: object) -> Session | None:
    """Return None for documents that are not exported conversations."""
    if not isinstance(doc, dict) or not isinstance(doc.get("turns"), list):
        return None
    if not isinstance(doc.get("source"), str) or not isinstance(doc.get("session"), str):
        return None
    messages = tuple(
        Message(turn["role"], turn["text"])
        for turn in doc["turns"]
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant")
        and isinstance(turn.get("text"), str) and turn["text"].strip()
    )
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
    return sorted(newest.values(), key=lambda s: s.mtime)
