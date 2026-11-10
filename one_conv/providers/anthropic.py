"""Read Claude Chat through an explicitly supplied private web session.

The authenticated web interface uses an offset-paged conversation listing and
message trees. These private routes are not a supported Anthropic API contract.
Cowork cloud uses a separate session-event adapter.
"""

from __future__ import annotations

from urllib.parse import quote
import json

from .base import (
    AuthenticationRequired,
    Conversation,
    ConversationPage,
    ConversationSummary,
    JsonTransport,
    Message,
    ProviderAccount,
    SchemaChanged,
)

FINGERPRINT = "claude-web-chat-2026-09-08"
CAPABILITIES = ("claude_chat:list", "claude_chat:read")
ROOT_MESSAGE_ID = "00000000-0000-4000-8000-000000000000"


def _text(value, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty):
        raise SchemaChanged(f"Claude response has an invalid {field}")
    return value


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise SchemaChanged("Claude response contains a non-object record")
    return value


def _optional_text(value, field: str) -> str | None:
    return None if value is None else _text(value, field)


class AnthropicProvider:
    """Keep account scope explicit instead of choosing the first organization."""

    def __init__(self, session, organization_id: str, *, transport=None, authenticated_account=None):
        self.organization_id = _text(organization_id, "organization ID")
        self.authenticated_account = authenticated_account
        self.transport = transport or JsonTransport(
            session, "https://claude.ai", FINGERPRINT
        )
        self._root = f"/api/organizations/{quote(organization_id, safe='')}/chat_conversations"

    def validate_connection(self) -> ProviderAccount:
        organizations = self.transport.get("/api/organizations")
        if not isinstance(organizations, list):
            raise SchemaChanged("Claude organization response must be an array")
        for raw in organizations:
            organization = _object(raw)
            identifier = _text(organization.get("uuid"), "organization ID")
            if identifier == self.organization_id:
                if self.authenticated_account is not None:
                    return self.authenticated_account
                return ProviderAccount(
                    id=identifier,
                    label=_text(organization.get("name"), "organization name", empty=True),
                    capabilities=CAPABILITIES,
                )
        raise AuthenticationRequired("The selected Claude organization is not accessible")

    def list_conversations(self, cursor=None, limit=50) -> ConversationPage:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit():
                raise ValueError("Invalid Claude listing cursor")
            offset = int(cursor)
            if offset > 2**31 - 1:
                raise ValueError("Invalid Claude listing cursor")
        payload = _object(self.transport.get(
            f"{self._root}_v2",
            params={"limit": limit, "offset": offset, "consistency": "eventual"},
        ))
        records = payload.get("data")
        if not isinstance(records, list) or type(payload.get("has_more")) is not bool:
            raise SchemaChanged("Claude listing requires data and a boolean has_more")
        if payload["has_more"] and not records:
            raise SchemaChanged("Claude listing cannot advance an empty page")
        summaries = []
        for raw in records:
            item = _object(raw)
            identifier = _text(item.get("uuid"), "conversation ID")
            summaries.append(ConversationSummary(
                id=identifier,
                title=_text(item.get("name"), "conversation name", empty=True),
                updated_at=_optional_text(item.get("updated_at"), "updated_at"),
                source_url=f"https://claude.ai/chat/{quote(identifier, safe='')}",
            ))
        # Advance by actual returned records so short pages cannot skip history.
        next_cursor = str(offset + len(records)) if payload["has_more"] else None
        return ConversationPage(items=tuple(summaries), next_cursor=next_cursor)

    def read_conversation(self, conversation_id: str) -> Conversation:
        identifier = _text(conversation_id, "conversation ID")
        payload = _object(self.transport.get(
            f"{self._root}/{quote(identifier, safe='')}",
            params={"tree": "True", "rendering_mode": "messages", "render_all_tools": "true",
                    "include_inline_comparison": "true", "consistency": "strong"},
        ))
        if payload.get("uuid") != identifier:
            raise SchemaChanged("Claude returned a different conversation")
        records = payload.get("chat_messages")
        if not isinstance(records, list):
            raise SchemaChanged("Claude chat_messages must be an array")
        messages = []
        seen = set()
        for raw in records:
            item = _object(raw)
            message_id = _text(item.get("uuid"), "message ID")
            if message_id in seen:
                raise SchemaChanged("Claude returned duplicate message IDs")
            seen.add(message_id)
            sender = item.get("sender")
            if sender not in ("human", "assistant"):
                raise SchemaChanged("Claude returned an unknown message sender")
            blocks = item.get("content")
            if not isinstance(blocks, list):
                raise SchemaChanged("Claude message content must be an array")
            blocks = list(blocks)
            text = []
            for raw_block in blocks:
                block = _object(raw_block)
                kind = _text(block.get("type"), "content type")
                if kind == "text":
                    text.append(_text(block.get("text"), "content text", empty=True))
                elif kind == "thinking":
                    # Keep source blocks without presenting reasoning as an answer.
                    _text(block.get("thinking"), "thinking content", empty=True)
                    text.append("[Thinking block retained in source content]")
                elif kind == "tool_use":
                    name = _text(block.get("name"), "tool name")
                    if not isinstance(block.get("input"), dict):
                        raise SchemaChanged("Claude tool input must be an object")
                    text.append(f"[Tool call: {name}]\n{json.dumps(block['input'], ensure_ascii=False)}")
                elif kind == "tool_result":
                    if not isinstance(block.get("content"), (list, str)) and not isinstance(block.get("text"), str):
                        raise SchemaChanged("Claude tool result has no recognized content")
                    text.append(f"[Tool result]\n{json.dumps(block.get('content', block.get('text')), ensure_ascii=False)}")
                elif kind in ("image", "document"):
                    # Inline pasted media has no text; the block itself stays in content_blocks.
                    text.append(f"[{kind.capitalize()} retained in source content]")
                else:
                    raise SchemaChanged(f"Claude returned an unsupported content block: {kind}")
            for field in ("attachments", "files"):
                assets = item.get(field, [])
                if not isinstance(assets, list):
                    raise SchemaChanged("Claude attachment metadata must be an array")
                for raw_asset in assets:
                    asset = _object(raw_asset)
                    name = _text(asset.get("file_name"), "attachment name")
                    blocks.append({"type": "source_attachment", "category": field, "metadata": asset})
                    text.append(f"[Attachment: {name}]")
                    if asset.get("extracted_content") is not None:
                        text.append(_text(asset["extracted_content"], "extracted attachment content", empty=True))
            parent = _optional_text(item.get("parent_message_uuid"), "parent ID")
            messages.append(Message(
                id=message_id,
                role="user" if sender == "human" else "assistant",
                text="\n".join(text),
                # Claude's synthetic root is not a missing transcript message.
                parent_id=None if parent == ROOT_MESSAGE_ID else parent,
                timestamp=_optional_text(item.get("created_at"), "created_at"),
                content_blocks=tuple(blocks),
            ))
        leaf = _optional_text(payload.get("current_leaf_message_uuid"), "active leaf")
        if leaf is not None and leaf not in seen:
            raise SchemaChanged("Claude active leaf is absent from the transcript")
        return Conversation(
            id=identifier,
            title=_text(payload.get("name"), "conversation name", empty=True),
            messages=tuple(messages),
            active_leaf_id=leaf,
        )
