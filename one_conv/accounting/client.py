"""Authenticated accounting transport bounds canonical JSON beneath the cloud body limit."""

import json
from urllib.parse import urlencode

from ..hr import RateError, _validate_card
from ..live import hubclient
from .model import AccountingError, normalize_event

MAX_BATCH_BYTES = 60_000


def _event_batches(events):
    """Measure the HubClient JSON encoding before any request can partially import invalid input."""
    batches, batch = [], []
    envelope_size = len(json.dumps({"events": []}).encode())
    body_size = envelope_size
    for event in events:
        event_size = len(json.dumps(event).encode())
        if envelope_size + event_size > MAX_BATCH_BYTES:
            raise AccountingError(f"Usage event {event['event_id']} exceeds the {MAX_BATCH_BYTES}-byte upload budget; "
                                  "shorten its provenance metadata before retrying")
        if batch and (len(batch) == 100 or body_size + 2 + event_size > MAX_BATCH_BYTES):
            batches.append(batch)
            batch, body_size = [], envelope_size
        # Default json.dumps inserts a comma and space between adjacent array elements.
        body_size += event_size + (2 if batch else 0)
        batch.append(event)
    if batch:
        batches.append(batch)
    return batches


def configured_client(use_hub=None, directory=None):
    """An unreachable configured authority never silently becomes a separate local ledger."""
    if use_hub is False:
        return None
    try:
        hub = hubclient.configured()
    except (hubclient.HubRefused, hubclient.HubUnavailable) as error:
        raise AccountingError(str(error)) from error
    if hub is None:
        if use_hub:
            raise AccountingError("No hub configured; connect the node with one-conv live serve or use --local")
        return None
    if directory is not None:
        raise AccountingError("--directory selects a local ledger; combine it with --local")
    return AccountingClient(hub)


class AccountingClient:
    """Canonical records cross the network; provider credentials and transcript text do not."""

    def __init__(self, hub):
        self.hub = hub

    def _request(self, method, path, body=None, key=None):
        try:
            result = self.hub.request(method, "/v1/accounting/" + path, body)
        except (hubclient.HubRefused, hubclient.HubUnavailable) as error:
            raise AccountingError(str(error)) from error
        if not isinstance(result, dict) or (key is not None and key not in result):
            raise AccountingError("Hub returned an invalid accounting response")
        return result[key] if key is not None else result

    def qualify(self, reference):
        if self.hub.node and ":" in reference and "/" not in reference.partition(":")[0]:
            return f"{self.hub.node}/{reference}"
        return reference

    def rates(self):
        rates = self._request("GET", "rates", key="rates")
        if not isinstance(rates, list):
            raise AccountingError("Hub rates response must be an array")
        return [_validate_card(card) for card in rates]

    def show_rate(self, reference):
        reference = self.qualify(reference)
        for card in self.rates():
            if reference == card.get("cost_center_id") or reference in (*card.get("aliases", []), *card.get("addresses", [])):
                return card
        raise RateError(f"No rate card for {reference!r}; run one-conv hr list")

    def register_rate(self, card, *, replace=False):
        card = {**card, "addresses": [self.qualify(address) for address in card["addresses"]]}
        return _validate_card(self._request("POST", "rates", {"card": card, "replace": replace}, key="card"))

    def quote(self, reference, minutes):
        result = self._request("POST", "quote", {"reference": self.qualify(reference), "minutes": minutes}, key="quote")
        if not isinstance(result, dict) or {"expected_cost", "hourly_rate", "cost_center_id", "name", "estimated_minutes", "notice"} - set(result):
            raise AccountingError("Hub quote response is incomplete")
        return result

    def _qualified_worker(self, value):
        if value is None:
            return None
        if "/" in value.partition(":")[0]:
            return value
        if not self.hub.node:
            raise AccountingError("Set ONE_CONV_HUB_NODE or provide a node-qualified worker ID before posting usage")
        return f"{self.hub.node}/{value}"

    def record_many(self, events):
        events = [normalize_event({**event, "worker_id": self._qualified_worker(event["worker_id"]),
                                   "parent_worker_id": self._qualified_worker(event.get("parent_worker_id"))}) for event in events]
        receipts = []
        for batch in _event_batches(events):
            result = self._request("POST", "events", {"events": batch}, key="receipts")
            if not isinstance(result, list) or len(result) != len(batch):
                raise AccountingError("Hub did not acknowledge every usage event; retry the same import")
            for sent, receipt in zip(batch, result, strict=True):
                if (not isinstance(receipt, dict) or receipt.get("status") not in ("recorded", "duplicate") or
                        not isinstance(receipt.get("event"), dict) or receipt["event"].get("event_id") != sent["event_id"]):
                    raise AccountingError("Hub usage receipt does not match the submitted identity; retry the same import")
            receipts.extend(result)
        return receipts

    def list_events(self, cost_center_id=None, **filters):
        filters["cost_center_id"] = cost_center_id
        query = urlencode({key: value for key, value in filters.items() if value is not None})
        events = self._request("GET", "usage" + ("?" + query if query else ""), key="events")
        if not isinstance(events, list):
            raise AccountingError("Hub usage response must be an array")
        return events

    def summary(self, cost_center_id=None, **filters):
        filters["cost_center_id"] = cost_center_id
        query = urlencode({key: value for key, value in filters.items() if value is not None})
        return self._request("GET", "summary" + ("?" + query if query else ""), key="summary")
