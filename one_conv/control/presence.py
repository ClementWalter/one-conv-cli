"""Present bridge, native-status and message evidence without claiming an agent read or answered."""

import math

from ..live.hub import LEASE_SECONDS


def _timestamp(value):
    """Bound epoch values before floating-point conversion or browser date rendering."""
    return value if type(value) in (int, float) and 0 < value < 32_503_680_000 and math.isfinite(value) else None


def session_presence(row, now):
    """A fresh bridge and publication permit messaging, without proving model availability."""
    bridge_seen = _timestamp(row.get("seen"))
    published = _timestamp(row.get("published_at", row.get("seen")))
    bridge_online = bridge_seen is not None and 0 <= now - bridge_seen <= LEASE_SECONDS
    fresh = (row.get("online") is True and bridge_online and published is not None
             and 0 <= now - published <= LEASE_SECONDS and not row.get("history_only"))
    status = row.get("status", "")
    if not fresh:
        state, label = "offline", "Offline"
        if row.get("history_only"):
            detail = "This conversation route is no longer published. Reopen the agent before sending a new message."
            published = None
        elif not bridge_online:
            detail = "The bridge is not connected. Reconnect it before sending a new message."
        else:
            detail = "The bridge is connected, but this agent's publication is stale. Reopen or reconnect the agent."
    elif status in ("idle", "busy"):
        state, label = status, "Idle" if status == "idle" else "Busy"
        detail = f"The connected bridge reports this agent is {status}. Model access and responsiveness are not verified."
    else:
        state, label = "unverified", "Unverified"
        detail = ("The bridge is connected. The native thread is reported loaded, but agent responsiveness is unverified. "
                  "A message may be queued." if status == "loaded" else
                  "The bridge is connected; the agent's current activity and responsiveness are unverified. A message may be queued.")
    return {"state": state, "label": label, "detail": detail, "last_seen": published,
            "bridge_last_seen": bridge_seen, "bridge_online": bridge_online, "can_message": fresh}


def message_activity(event):
    """A literal last-message excerpt supplies context, never an inferred current task."""
    if event is None:
        return {"kind": "unknown", "label": "No workspace message recorded", "at": None,
                "direction": None, "message_id": None, "topic": None, "peer": None, "summary": None}
    summary = event["text"][:240]
    if len(event["text"]) > 240:
        summary += "…"
    return {"kind": "message", "label": "Last message sent" if event["direction"] == "outgoing" else "Last message received",
            "at": event["at"], "direction": event["direction"], "message_id": event["id"],
            "topic": event["topic"], "peer": event["peer"], "summary": summary}


def message_delivery(outcome, outcome_at=None, *, human_inbox=False):
    """Adapter acceptance and a durable human inbox carry no read or reply receipt."""
    state = outcome or "stored"
    labels = {"stored": "Stored on the hub", "accepted": "Accepted by adapter", "refused": "Delivery refused",
              "unconfirmed": "Delivery unconfirmed"}
    details = {"stored": "The hub stored this message; the receiving adapter has not confirmed acceptance.",
               "accepted": "The receiving adapter accepted this message. Reading and a reply are not confirmed.",
               "refused": "The receiving adapter refused this delivery. It has not confirmed reading or a reply.",
               "unconfirmed": "The receiving adapter could not confirm delivery. The message may already have reached the agent."}
    label, detail = labels.get(state, "Delivery unknown"), details.get(state, "No delivery evidence is available.")
    if human_inbox and state == "accepted":
        label, detail = "Stored in your inbox", "The workspace stored this message in your inbox. This is not a read receipt."
    return {"state": state, "label": label, "detail": detail,
            "accepted_at": _timestamp(outcome_at) if state == "accepted" else None, "read_at": None}
