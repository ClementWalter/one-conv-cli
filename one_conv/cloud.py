"""Persist normalized cloud reads without coupling indexing to private API shapes."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .providers.base import ProviderError, SchemaChanged


def cache_root() -> Path:
    return Path(os.environ.get("ONE_CONV_CLOUD_CACHE", str(Path.home() / ".cache/one-conv-cli/cloud")))


def _key(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _write(path: Path, document: dict) -> None:
    # Atomic replacement preserves the last usable transcript on interrupted reads.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(document, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _timestamp(value) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    return value if isinstance(value, str) else ""


def _active_messages(conversation):
    if conversation.active_leaf_id is None:
        return conversation.messages
    by_id = {row.id: row for row in conversation.messages}
    selected = []
    seen = set()
    current = conversation.active_leaf_id
    while current is not None:
        if current in seen:
            raise SchemaChanged("Conversation branch contains a cycle")
        seen.add(current)
        row = by_id.get(current)
        if row is None:
            raise SchemaChanged("Conversation branch references a missing message")
        selected.append(row)
        current = row.parent_id
    return tuple(row for row in reversed(selected) if row.role != "structure")


def _unchanged(destination: Path, updated_at) -> bool:
    """A complete cached copy with the listing's update time needs no new read."""
    stamp = _timestamp(updated_at)
    if not stamp or not destination.exists():
        return False
    try:
        cached = json.loads(destination.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(cached, dict) and cached.get("complete", True) is not False and cached.get("last") == stamp


def synchronize(provider, product: str, *, root: Path | None = None, limit: int = 100) -> dict:
    """Bound each discovery run and retain cached content when a provider drifts."""
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be between 1 and 10000")
    account = provider.validate_connection()
    directory = (root or cache_root()) / _key(product, account.id)
    report = {"product": product, "account_id": account.id, "fetched": 0,
              "incomplete_conversations": 0, "retained_complete_snapshots": 0, "unchanged": 0,
              "state": "syncing", "capabilities": account.capabilities,
              "observed_at": datetime.now(timezone.utc).isoformat()}
    cursor = None
    cursors = set()
    seen = set()
    pages = 0
    try:
        while report["fetched"] < limit:
            pages += 1
            if pages > limit + 10:
                raise SchemaChanged("Provider exceeded the discovery page budget")
            page = provider.list_conversations(cursor=cursor, limit=min(50, limit - report["fetched"]))
            truncated = len(page.items) > limit - report["fetched"]
            for item in page.items:
                if item.id in seen:
                    raise SchemaChanged("Repeated conversation identity in provider listing")
                seen.add(item.id)
                # Rate-limited providers throttle per-conversation reads, so reruns only read what changed.
                if _unchanged(directory / f"{_key(product, account.id, item.id)}.json", item.updated_at):
                    report["unchanged"] += 1
                    report["fetched"] += 1
                    if report["fetched"] >= limit:
                        break
                    continue
                conversation = provider.read_conversation(item.id)
                if conversation.id != item.id:
                    raise SchemaChanged("Conversation identity differs from requested identity")
                document = asdict(conversation)
                if not conversation.complete:
                    report["incomplete_conversations"] += 1
                document.update({"source": product, "machine": "cloud",
                                 "account_id": account.id,
                                 "account_label": account.label,
                                 "session": _key(product, account.id, conversation.id),
                                 "cwd": f"{product}:{account.id}",
                                 "source_url": item.source_url,
                                 "last": _timestamp(item.updated_at) or report["observed_at"],
                                 "observed_at": report["observed_at"],
                                 "turns": [{"role": row.role, "text": row.text,
                                            "ts": _timestamp(row.timestamp), "id": row.id,
                                            "parent_id": row.parent_id,
                                            "content_blocks": row.content_blocks}
                                           for row in _active_messages(conversation)]})
                destination = directory / f"{document['session']}.json"
                retain = False
                if not conversation.complete and destination.exists():
                    try:
                        retain = json.loads(destination.read_text()).get("complete", True)
                    except (OSError, ValueError, AttributeError):
                        retain = False
                # A bounded refresh must not replace an already complete history window.
                if retain:
                    report["retained_complete_snapshots"] += 1
                else:
                    _write(destination, document)
                report["fetched"] += 1
                if report["fetched"] >= limit:
                    break
            if page.next_cursor is None:
                report["state"] = "partial" if truncated or report["incomplete_conversations"] else "ready"
                break
            if page.next_cursor in cursors:
                raise SchemaChanged("Provider pagination did not advance")
            cursors.add(page.next_cursor)
            cursor = page.next_cursor
        if report["state"] == "syncing":
            report["state"] = "partial"
    except ProviderError as error:
        report.update(state="error", error=type(error).__name__)
        _write(directory / ".status.json", report)
        raise
    _write(directory / ".status.json", report)
    return report
