"""Build a local, self-contained HTML viewer of the dataset's conversations and their labels.

The page embeds full conversation text, so it is written to the tune workspace
and opened from disk rather than published anywhere.
"""

import json
from datetime import datetime
from pathlib import Path

from .corpus import Session, merge_runs
from .label import moment_key, moment_state
from .mood import intensity, session_heat

_MAX_MESSAGE_CHARS = 8_000


def _probability(row: dict) -> float:
    return round(row["probabilities"].get(row["label"], 0.0), 2)


def session_record(session: Session, reactions: dict[str, dict], moods: dict[str, dict]) -> dict:
    """One session with per-reply reaction labels and per-reaction tone levels."""
    messages = merge_runs(session.messages)
    rendered = []
    levels = []
    for index, message in enumerate(messages):
        text = message.text if len(message.text) <= _MAX_MESSAGE_CHARS else message.text[:_MAX_MESSAGE_CHARS] + "\n[…]"
        rendered.append({"role": message.role, "text": text})
    for index in range(1, len(messages) - 1):
        if messages[index].role != "assistant" or messages[index + 1].role != "user":
            continue
        key = moment_key(moment_state(messages, index))
        # The label describes the user's next message, so it is shown there.
        if key in reactions:
            rendered[index + 1]["reaction"] = [reactions[key]["label"], _probability(reactions[key])]
        if key in moods:
            level = round(intensity(moods[key]), 2)
            rendered[index + 1]["mood"] = level
            levels.append(level)
    first = next((m.text for m in messages if m.role == "user"), "")
    return {"id": session.key, "source": session.source, "cwd": session.cwd,
            "date": datetime.fromtimestamp(session.mtime).date().isoformat() if session.mtime else "",
            "title": " ".join(first.split())[:120], "messages": rendered, **session_heat(levels)}


def write_viewer(sessions: list[Session], reactions: dict[str, dict], moods: dict[str, dict], out: Path) -> int:
    records = [session_record(s, reactions, moods) for s in sessions if s.messages]
    # Escaping "</" keeps message text from closing the data script tag.
    payload = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")
    template = (Path(__file__).parent / "viewer.html").read_text(encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    return len(records)
