"""Shared session record, addresses, state directory and the delivered message envelope."""

import html
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

HARNESSES = ("claude", "codex", "omp", "cursor", "boat")


@dataclass(frozen=True)
class LiveSession:
    """One running session; `endpoint` is adapter-specific (socket path, inbox dir, ...)."""

    harness: str
    id: str
    cwd: str = ""
    title: str = ""
    status: str = ""
    pid: int | None = None
    delivery: str = ""
    endpoint: str = field(default="", compare=False)

    @property
    def address(self) -> str:
        return f"{self.harness}:{self.id}"

    def to_json(self) -> dict:
        row = asdict(self)
        row["address"] = self.address
        return row


def parse_address(text: str) -> tuple[str | None, str]:
    """Split `harness:id`; a bare string is a query over every harness."""
    harness, sep, rest = text.partition(":")
    if sep and harness in HARNESSES and rest:
        return harness, rest
    return None, text


# Session ids become file and directory names under live_home(); anything else could escape it.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def is_session_id(value: str) -> bool:
    return bool(_SESSION_ID.fullmatch(value)) and ".." not in value


def live_home() -> Path:
    return Path(os.environ.get("ONE_CONV_LIVE_DIR", str(Path.home() / ".config" / "agent-conv-cli" / "live")))


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# A body may not open or close an envelope, or it could forge a second block from another sender.
_ENVELOPE_TAG = re.compile(r"<(\s*/?\s*one-conv-message)", re.IGNORECASE)


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def envelope(text: str, sender: LiveSession | None, origin: str = "unknown", reply: str = "expected") -> str:
    """Wrap a message so the receiver knows who sent it and whether an answer is wanted.

    `origin` labels a sender that is not an agent session: "user" only for an
    interactive terminal, "unknown" for anything else (a detached or scripted process).
    `reply="none"` marks a notice that ends an exchange: it carries no reply instruction.
    `reply="inline"` is for a receiver the sender reads back from (a Boat sandbox), which
    answers in its own conversation because it cannot message this machine.
    """
    text = _ENVELOPE_TAG.sub(r"&lt;\1", text)
    if reply not in ("expected", "none", "inline"):
        raise ValueError(f"reply must be 'expected', 'none' or 'inline', got {reply!r}")
    if sender is None:
        attrs = f'from="{_attr(origin)}"'
    else:
        attrs = f'from="{_attr(sender.address)}"'
        if sender.title:
            attrs += f' from-name="{_attr(sender.title)}"'
        if sender.cwd:
            attrs += f' from-cwd="{_attr(sender.cwd)}"'
    footer = ""
    if reply == "none":
        attrs += ' reply="none"'
        footer = "\nNo reply expected. Do not reply to this message."
    elif reply == "inline":
        footer = "\nAnswer in this conversation; the sender reads your reply back from here."
    elif sender is not None and not sender.id.startswith("?"):
        footer = f'\nTo reply, call the one-conv send_message tool with to="{_attr(sender.address)}".'
    return f"<one-conv-message {attrs}>\n{text}\n</one-conv-message>{footer}"
