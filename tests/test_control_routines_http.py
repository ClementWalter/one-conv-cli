"""Over loopback HTTP, any node reads routines, and only a node with staffing authority changes them."""

import pytest

from one_conv.live.hubclient import HubRefused
from tests import test_control_authority_http as authority

workspace = authority.workspace

ROUTINE = {"id": "c" * 32, "name": "Standup", "topic": "team", "prompt": "Post the standup.",
           "cron": "30 9 * * 1-5", "tz": "Europe/Paris", "enabled": True}


def test_any_node_reads_routines(workspace):
    _, client = workspace
    assert client.request("GET", "/v1/routines") == {"routines": []}


def test_a_node_without_staffing_authority_cannot_add_a_routine(workspace):
    _, client = workspace
    with pytest.raises(HubRefused, match=r"\(403\)"):
        client.request("POST", "/v1/routines", {"action": "save", "routine": ROUTINE})


def test_a_node_without_staffing_authority_cannot_run_a_routine(workspace):
    app, client = workspace
    app.state.store.routines.save(ROUTINE)
    with pytest.raises(HubRefused, match=r"\(403\)"):
        client.request("POST", "/v1/routines", {"id": ROUTINE["id"], "action": "run"})


def test_a_node_with_staffing_authority_adds_a_routine(workspace):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    assert client.request("POST", "/v1/routines", {"action": "save", "routine": ROUTINE})["schedule"] == "Weekdays at 09:30"


def test_a_node_with_staffing_authority_pauses_a_routine(workspace):
    app, client = workspace
    app.state.store.grant_staffing("cfo")
    app.state.store.routines.save(ROUTINE)
    assert client.request("POST", "/v1/routines", {"id": ROUTINE["id"], "action": "pause"})["enabled"] is False
