#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
#     "mcp==2.2.0",
#     "websockets==17.2",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
#     "httpx==0.28.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Managed staffing preserves browser and node authority boundaries."""

import stat
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from one_conv.control.cli import build_control_group
from one_conv.control.runtime import Teammates, application
from one_conv.live.cli import build_live_group

ORIGIN = "https://workspace.example.test"


@pytest.fixture
def app(tmp_path):
    return application(tmp_path, ORIGIN)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as active:
        yield active


@pytest.fixture
def node(client, app):
    token = app.state.hub.add_token("worker")
    client.headers["Authorization"] = "Bearer " + token
    return client


def test_a_node_reads_no_teammate_listing(node):
    assert node.get("/v1/workers").status_code == 405


@pytest.fixture
def teammates(tmp_path):
    """The hub, with the Mac's bridge offering to start claude sessions."""
    from one_conv.live.hub import Hub
    hub = Hub(str(tmp_path / "hub.db"))
    hub.publish("mac", [], spawn="claude", priority=10)
    return SimpleNamespace(hub=hub, teammates=Teammates(hub))


TEAMMATE = {"name": "jack", "topic": "debate", "prompt": "Join and defend one of the theses"}


def test_a_new_teammate_is_started_by_a_spawner(teammates):
    result = teammates.teammates.browser("POST", {"action": "spawn", "id": "e" * 32, "spec": TEAMMATE})
    assert result["route"] == "mac/spawn:claude"


def test_a_new_teammate_answers_with_its_request_id(teammates):
    result = teammates.teammates.browser("POST", {"action": "spawn", "id": "e" * 32, "spec": TEAMMATE})
    assert result["id"] == "e" * 32


def test_the_teammate_request_is_posted_in_its_channel(teammates):
    teammates.teammates.browser("POST", {"action": "spawn", "id": "e" * 32, "spec": TEAMMATE})
    assert [row["text"] for row in teammates.hub.history("debate")] == ["New teammate @jack: Join and defend one of the theses"]


@pytest.mark.parametrize("spec", [{**TEAMMATE, "topic": "Bad Topic"}, {**TEAMMATE, "name": "Jack!"},
                                  {**TEAMMATE, "prompt": " "}, {**TEAMMATE, "harness": "codex"}, "jack"])
def test_a_malformed_teammate_is_refused(teammates, spec):
    with pytest.raises(ValueError):
        teammates.teammates.browser("POST", {"action": "spawn", "id": "e" * 32, "spec": spec})


@pytest.mark.parametrize("action", ["stop", "recheck", "stop_pool", "resume_pool", "reconcile"])
def test_only_spawn_is_a_teammate_action(teammates, action):
    with pytest.raises(ValueError):
        teammates.teammates.browser("POST", {"action": action, "id": "e" * 32})


def test_pairing_cli_writes_a_private_link(tmp_path):
    path = tmp_path / "phone.link"
    CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path / "state"), "pair", "--origin", ORIGIN,
                                               "--output", str(path)])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pairing_cli_does_not_echo_the_secret(tmp_path):
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path / "state"), "pair", "--origin", ORIGIN,
                                                       "--output", str(tmp_path / "phone.link")])
    assert "#pair=" not in result.output


def test_node_cli_refuses_the_human_mailbox_identity(tmp_path):
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path), "node", "add", "workspace",
                                                       "--output", str(tmp_path / "key")])
    assert result.exit_code == 1


@pytest.fixture
def teammate_spawn(monkeypatch):
    from one_conv.control import client
    calls = []

    def request_teammate(spec, ident):
        calls.append({"spec": spec, "id": ident})
        return {"id": ident, "status": "node", "route": "mac/spawn:claude"}

    monkeypatch.setattr(client, "request_teammate", request_teammate)
    result = CliRunner().invoke(build_live_group([]), ["spawn", "claude", "Review the change.",
                                                      "--name", "jack", "--topic", "release", "--yes", "--json"])
    return calls, result


def test_a_topic_spawn_does_not_start_a_local_process(teammate_spawn):
    assert len(teammate_spawn[0]) == 1


def test_a_topic_spawn_preserves_the_requested_task(teammate_spawn):
    assert teammate_spawn[0][0]["spec"] == {"name": "jack", "topic": "release", "prompt": "Review the change."}


def test_a_topic_spawn_returns_its_durable_request_id(teammate_spawn):
    assert teammate_spawn[0][0]["id"] in teammate_spawn[1].output


def test_a_topic_spawn_needs_a_name():
    result = CliRunner().invoke(build_live_group([]), ["spawn", "claude", "Review the change.", "--topic", "release", "--yes"])
    assert result.exit_code == 2