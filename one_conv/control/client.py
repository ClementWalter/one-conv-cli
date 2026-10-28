"""Teammate requests and routines go to the machine's configured hub, with stable request IDs."""

from ..live import hubclient


def routines(method="GET", payload=None):
    client = hubclient.configured()
    if client is None:
        raise ValueError("Configure live serve or ONE_CONV_HUB_URL/TOKEN before using routines")
    return client.request(method, "/v1/routines", payload)


def request_teammate(spec, ident):
    """An unknown response leaves the durable ID with the caller for an explicit retry."""
    client = hubclient.configured()
    if client is None:
        raise ValueError("Configure live serve or ONE_CONV_HUB_URL/TOKEN before asking for a teammate")
    try:
        result = client.request("POST", "/v1/workers", {"action": "spawn", "id": ident, "spec": spec})
    except hubclient.HubUnavailable:
        raise ValueError(f"The outcome is unknown. Retry the same request with --request-id {ident}.") from None
    if not isinstance(result, dict) or result.get("id") != ident or not isinstance(result.get("status"), str):
        raise ValueError(f"The hub returned no receipt. Retry with --request-id {ident}.")
    return result
