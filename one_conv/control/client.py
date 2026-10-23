"""Managed cloud staffing uses stable request IDs and the machine's configured hub."""

from ..live import hubclient


def workers(method="GET", payload=None):
    client = hubclient.configured()
    if client is None:
        raise ValueError("Configure live serve or ONE_CONV_HUB_URL/TOKEN before using cloud workers")
    return client.request(method, "/v1/workers", payload)


def routines(method="GET", payload=None):
    client = hubclient.configured()
    if client is None:
        raise ValueError("Configure live serve or ONE_CONV_HUB_URL/TOKEN before using routines")
    return client.request(method, "/v1/routines", payload)


def submit(spec, ident):
    """An unknown response leaves the durable ID with the caller for an explicit retry."""
    try:
        result = workers("POST", {"action": "spawn", "id": ident, "spec": spec})
    except hubclient.HubUnavailable:
        raise ValueError(f"Cloud outcome is unknown. Retry the same request with --request-id {ident}; do not choose another name.") from None
    if not isinstance(result, dict) or result.get("id") != ident or not isinstance(result.get("status"), str):
        raise ValueError(f"Cloud returned no job receipt. Retry with --request-id {ident}.")
    return result
