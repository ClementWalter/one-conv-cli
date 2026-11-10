"""Read organization-scoped Cowork cloud sessions as event snapshots, and post user messages to them."""

import json
import time
from urllib.parse import quote
import uuid

from .anthropic import AnthropicProvider, SentMessage, _object, _text
from .base import (Conversation, ConversationPage, ConversationSummary, JsonTransport, Message,
                   ProviderUnavailable, SchemaChanged)

CAPABILITIES = ("cowork:list", "cowork:read_events")
FINGERPRINT = "cowork-session-events:2026-09-08"


def _page(payload):
    payload = _object(payload)
    if not isinstance(payload.get("data"), list):
        raise SchemaChanged("Cowork page requires a data array")
    cursor = payload.get("next_cursor")
    if cursor is not None:
        cursor = _text(cursor, "next cursor")
        if not payload["data"]:
            raise SchemaChanged("Cowork pagination cannot advance an empty page")
    return payload["data"], cursor


def _message(event):
    if event.get("event_type") not in ("user", "assistant"):
        return None
    payload = _object(event.get("payload"))
    message = _object(payload.get("message"))
    role = message.get("role")
    if role not in ("user", "assistant"):
        raise SchemaChanged("Cowork message has an unknown role")
    content = message.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content
    else:
        raise SchemaChanged("Cowork message has invalid content")
    text = []
    for raw in blocks:
        block = _object(raw)
        kind = _text(block.get("type"), "block type")
        if kind == "text":
            text.append(_text(block.get("text"), "text", empty=True))
        elif kind in ("thinking", "redacted_thinking"):
            text.append("[Thinking block retained in source content]")
        elif kind == "tool_use":
            text.append(f"[Tool call: {_text(block.get('name'), 'tool name')}]\n{json.dumps(block.get('input'), ensure_ascii=False)}")
        elif kind == "tool_result":
            text.append("[Tool result]\n" + json.dumps(block.get("content"), ensure_ascii=False))
        elif kind in ("image", "document"):
            text.append(f"[{kind.capitalize()} retained in source content]")
        else:
            raise SchemaChanged(f"Cowork returned an unsupported content block: {kind}")
    return Message(_text(event.get("event_id"), "event ID"), role, "\n".join(text),
                   timestamp=event.get("created_at"), content_blocks=tuple(blocks))


class CoworkProvider:
    """Keep volatile session protocol separate from shared storage and readers."""

    def __init__(self, session, organization_id, *, authenticated_account):
        self.identity = AnthropicProvider(session, organization_id, authenticated_account=authenticated_account)
        session.headers.update({"x-organization-uuid": organization_id,
                                "anthropic-version": "2023-06-01",
                                "anthropic-beta": "ccr-byoc-2025-07-29",
                                "anthropic-client-feature": "ccr"})
        self.transport = JsonTransport(session, "https://claude.ai", FINGERPRINT)

    def validate_connection(self):
        return self.identity.validate_connection()

    def list_conversations(self, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params = {"limit": min(limit, 50), "tags": "cowork-remote"}
        if cursor is not None:
            params["cursor"] = _text(cursor, "cursor")
        rows, following = _page(self.transport.get("/v1/code/sessions", params))
        summaries = []
        for raw in rows:
            row = _object(raw)
            if "cowork-remote" not in row.get("tags", []):
                raise SchemaChanged("Cowork listing returned a different session product")
            identifier = _text(row.get("id"), "session ID")
            summaries.append(ConversationSummary(identifier, _text(row.get("title"), "title", empty=True),
                                                row.get("updated_at"), f"https://claude.ai/cowork/{quote(identifier, safe='')}"))
        return ConversationPage(tuple(summaries), following)

    def read_conversation(self, conversation_id):
        root = "/v1/code/sessions/" + quote(_text(conversation_id, "session ID"), safe="")
        detail = _object(self.transport.get(root))
        detail = _object(detail.get("response_shape", detail))
        if detail.get("id") != conversation_id or "cowork-remote" not in detail.get("tags", []):
            raise SchemaChanged("Cowork session identity or product mismatch")
        events = self._events(root)
        # Events are a snapshot, including pending tool questions, not a completed task.
        messages = tuple(message for _, event in events if (message := _message(event)) is not None)
        return Conversation(conversation_id, _text(detail.get("title"), "title", empty=True),
                            messages, coverage="session_event_snapshot", complete=True)

    def _events(self, root):
        """Every event of a session as (sequence number, event), oldest first."""
        events, cursors, seen = [], set(), set()
        cursor = None
        for _ in range(100):
            params = {"limit": 500, "sort_order": "asc"}
            if cursor is not None:
                params["cursor"] = cursor
            rows, following = _page(self.transport.get(root + "/events", params))
            for raw in rows:
                event = _object(raw)
                identifier = _text(event.get("event_id"), "event ID")
                if identifier in seen:
                    raise SchemaChanged("Cowork repeated an event identity")
                seen.add(identifier)
                sequence = str(event.get("sequence_num", ""))
                if not sequence.isascii() or not sequence.isdigit():
                    raise SchemaChanged("Cowork event sequence is invalid")
                events.append((int(sequence), event))
            if following is None:
                break
            if following in cursors:
                raise SchemaChanged("Cowork event pagination did not advance")
            cursors.add(following)
            cursor = following
        else:
            raise SchemaChanged("Cowork event pagination exceeded its page budget")
        return sorted(events, key=lambda item: item[0])

    def send_message(self, conversation_id, text, *, timeout=600, interval=5, sleep=time.sleep,
                     clock=time.monotonic):
        """Post TEXT as the user's next message in a Cowork session and wait for the turn to end.

        The session's agent runs the turn in the cloud; its reply is the assistant text
        between the posted event and the `result` event that closes the turn.
        """
        identifier = _text(conversation_id, "session ID")
        root = "/v1/code/sessions/" + quote(identifier, safe="")
        event_uuid = str(uuid.uuid4())
        payload = {"type": "user", "uuid": event_uuid, "session_id": identifier, "parent_tool_use_id": None,
                   "message": {"role": "user", "content": _text(text, "message text")}}
        answer = _object(self.transport.post_session_events(root + "/events", {"events": [{"payload": payload}]}))
        results = answer.get("results")
        if not isinstance(results, list) or len(results) != 1:
            raise SchemaChanged("Cowork did not acknowledge exactly one event")
        result = _object(results[0])
        sequence = str(result.get("sequence_num", ""))
        if not sequence.isdigit():
            raise SchemaChanged("Cowork acknowledged the event without a sequence number")
        posted = int(sequence)
        deadline = clock() + timeout
        while True:
            reply, outcome = [], None
            for number, event in self._events(root):
                if number <= posted:
                    continue
                if event.get("event_type") == "assistant":
                    message = _message(event)
                    if message is not None and message.text:
                        reply.append(message.text)
                elif event.get("event_type") == "result":
                    outcome = _object(event.get("payload")).get("subtype") or "result"
                    break
            if outcome is not None:
                return SentMessage(conversation_id=identifier,
                                   message_id=_text(result.get("event_id"), "event ID"),
                                   reply_id=None, reply="\n\n".join(reply), stop_reason=outcome)
            if clock() >= deadline:
                raise ProviderUnavailable("Cowork's turn did not finish in time; the message is posted, "
                                          "read the session later for the reply")
            sleep(interval)
