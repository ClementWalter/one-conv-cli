"""Shared session record, addresses, state directory and the delivered message envelope."""

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

HARNESSES = ("claude", "codex", "omp", "cursor")


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


def envelope(text: str, sender: LiveSession | None, origin: str = "unknown") -> str:
    """Wrap a message so the receiver knows who sent it and how to answer.

    `origin` labels a sender that is not an agent session: "user" only for an
    interactive terminal, "unknown" for anything else (a detached or scripted process).
    """
    if sender is None:
        return f'<one-conv-message from="{origin}">\n{text}\n</one-conv-message>'
    attrs = f'from="{sender.address}"'
    if sender.title:
        attrs += f' from-name="{sender.title.replace(chr(34), chr(39))}"'
    if sender.cwd:
        attrs += f' from-cwd="{sender.cwd}"'
    reply = ""
    if not sender.id.startswith("?"):
        reply = f'\nTo reply, call the one-conv send_message tool with to="{sender.address}".'
    return f"<one-conv-message {attrs}>\n{text}\n</one-conv-message>{reply}"
