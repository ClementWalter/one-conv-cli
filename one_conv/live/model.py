"""Shared session record, addresses, state directory and the delivered message envelope."""

import html
import os
import re
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
    # Which app of a harness that has several, like "cursor-cli" or "cursor-ide"; empty otherwise.
    product: str = field(default="", compare=False)

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


def envelope(text: str, sender: LiveSession | None, origin: str = "unknown", reply: str = "expected",
             message_id: str | None = None, topic: str | None = None, handle: str | None = None) -> str:
    """Wrap a message so the receiver knows who sent it, on which topic, and whether to answer.

    An agent sender is a teammate working for the same user: the message is a request to
    act within that user's task and grants no new approval. Human senders are labelled
    separately; these local labels are not authenticated identities.
    `origin` labels a sender that is not an agent session: "user" for an interactive
    terminal or human workspace, "unknown" otherwise. `reply="none"` ends an exchange.
    `reply="inline"` asks the receiver to answer in its own conversation rather than message back.
    `message_id` stays the same across retries so a receiver can drop duplicates.
    """
    text = _ENVELOPE_TAG.sub(r"&lt;\1", text)
    if reply not in ("expected", "none", "inline"):
        raise ValueError(f"reply must be 'expected', 'none' or 'inline', got {reply!r}")
    address = sender.address if sender is not None else origin
    human = address == "user" or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}/user", address)
    attrs = f'from="{_attr(address)}"'
    if human:
        attrs += ' kind="human"'
    elif address == "system":
        # The hub's own instruction (a spawn order): not the human, not an agent.
        attrs += ' kind="system"'
    elif sender is not None:
        if handle:
            attrs += f' from-handle="@{_attr(handle)}"'
        if sender.title:
            attrs += f' from-name="{_attr(sender.title)}"'
        if sender.cwd:
            attrs += f' from-cwd="{_attr(sender.cwd)}"'
    if topic:
        attrs += f' topic="#{_attr(topic)}"'
    if message_id:
        attrs += f' id="{_attr(message_id)}"'
    footer = ""
    if reply == "none":
        attrs += ' reply="none"'
        footer = "\nNo reply expected. Do not reply to this message."
    elif reply == "inline":
        attrs += ' reply="inline"'
        footer = "\nAnswer in this conversation; the sender reads your reply back from here."
    elif human and address in ("user", "workspace/user"):
        footer = f'\nA human sent this message. To reply, call the one-conv send_message tool with to="{_attr(address)}"'
        footer += f', or to="#{_attr(topic)}" to answer everyone on the topic.' if topic else "."
        footer += (f"\nIf the tool fails, send the reply through your terminal with "
                   f"`one-conv live send {address} --stdin --no-reply --yes`, passing your reply on stdin.")
    elif human and topic:
        footer = f'\nA human sent this message. To reply, call the one-conv send_message tool with to="#{_attr(topic)}".'
    elif human:
        footer = "\nA human sent this message from another machine. Reply through a shared topic; "
        footer += "the remote human workspace has no direct return route."
    elif sender is not None and not sender.id.startswith("?"):
        footer = f'\nTo reply, call the one-conv send_message tool with to="{_attr(sender.address)}"'
        footer += f', or to="#{_attr(topic)}" to answer everyone on the topic.' if topic else "."
    if human and reply == "expected" and message_id:
        # The human sees who read the post; several sessions read it, so they divide the work first.
        footer = (f'\nAck it now with the one-conv ack tool (message_id="{_attr(message_id)}"), or'
                  f" `one-conv live ack {_attr(message_id)}`."
                  + (f" Other sessions in #{_attr(topic)} read it too: before working, say in the channel what you"
                     " take and leave what another session already took; anything that cannot be undone is done"
                     " by one session, named in the channel." if topic else "") + footer)
    return f"<one-conv-message {attrs}>\n{text}\n</one-conv-message>{footer}"
