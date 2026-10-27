#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
"""Staffing delegation belongs to one credential generation, separate from peer messaging."""

import hashlib
import stat

import pytest
from click.testing import CliRunner
from starlette.requests import Request
from starlette.testclient import TestClient

from one_conv.control.cli import build_control_group
from one_conv.control.runtime import Workers, application
from one_conv.control.store import ControlStore
from one_conv.live.hub import HubError

ORIGIN = "https://workspace.example.test"
MUTATIONS = ["spawn", "stop", "recheck", "stop_pool", "resume_pool", "reconcile"]


def bearer(token):
    return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})


@pytest.fixture
def store(tmp_path):
    return ControlStore(tmp_path)


@pytest.fixture
def dispatched(monkeypatch):
    calls = []

    def browser(self, method, payload):
        calls.append((method, payload))
        return {"method": method, "action": payload.get("action")}

    monkeypatch.setattr(Workers, "browser", browser)
    return calls


@pytest.fixture
def app(tmp_path, dispatched):
    return application(tmp_path, ORIGIN, worker_config={})


@pytest.fixture
def client(app):
    # No lifespan is needed: request tests must never advance provisioning jobs.
    return TestClient(app, base_url=ORIGIN)


@pytest.fixture
def token(app):
    return app.state.hub.add_token("cfo")


@pytest.mark.parametrize("action", MUTATIONS)
def test_ordinary_node_cannot_mutate_staffing(client, token, action):
    response = client.post("/v1/workers", json={"action": action}, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403


@pytest.mark.parametrize("action", MUTATIONS)
def test_denied_staffing_does_not_dispatch(client, token, dispatched, action):
    client.post("/v1/workers", json={"action": action}, headers={"Authorization": "Bearer " + token})
    assert dispatched == []


def test_node_can_read_staffing_without_delegation(client, token):
    assert client.get("/v1/workers", headers={"Authorization": "Bearer " + token}).status_code == 200


@pytest.mark.parametrize("action", MUTATIONS)
def test_operator_delegation_allows_each_staffing_mutation(client, app, token, action):
    app.state.store.grant_staffing("cfo")
    assert client.post("/v1/workers", json={"action": action}, headers={"Authorization": "Bearer " + token}).status_code == 200


def test_paired_human_keeps_staffing_access(client, app):
    client.post("/api/login", json={"token": app.state.auth.issue_pairing_token()}, headers={"Origin": ORIGIN}).raise_for_status()
    csrf = client.get("/api/session").json()["csrf_token"]
    assert client.post("/api/providers/workers", json={"action": "spawn"},
                       headers={"Origin": ORIGIN, "X-One-Conv-Token": csrf}).status_code == 200


def test_browser_cookie_does_not_authorize_node_staffing(client, app):
    client.post("/api/login", json={"token": app.state.auth.issue_pairing_token()}, headers={"Origin": ORIGIN}).raise_for_status()
    assert client.post("/v1/workers", json={"action": "spawn"}).status_code == 401


def test_pool_minted_token_has_no_staffing_authority(client, app):
    token = app.state.workers.manager._token({"id": "a" * 32, "node": "resident-pool"})
    assert client.post("/v1/workers", json={"action": "spawn"}, headers={"Authorization": "Bearer " + token}).status_code == 403


def test_staffing_payload_cannot_self_delegate(client, token):
    assert client.post("/v1/workers", json={"action": "spawn", "staffing": True},
                       headers={"Authorization": "Bearer " + token}).status_code == 403


def test_revoke_staffing_keeps_peer_credential_valid(store):
    token = store.hub.add_token("cfo")
    store.grant_staffing("cfo")
    store.revoke_staffing("cfo")
    assert store.hub.node_for(token) == "cfo"


def test_staffing_revocation_denies_new_requests(client, app, token):
    app.state.store.grant_staffing("cfo")
    app.state.store.revoke_staffing("cfo")
    assert client.post("/v1/workers", json={"action": "stop"}, headers={"Authorization": "Bearer " + token}).status_code == 403


def test_grant_survives_store_restart(store):
    token = store.hub.add_token("cfo")
    store.grant_staffing("cfo")
    assert ControlStore(store.directory).staffing_node(bearer(token)) == "cfo"


def test_delegation_stores_only_token_fingerprint(store):
    token = store.hub.add_token("cfo")
    store.grant_staffing("cfo")
    assert token.encode() not in store.directory.joinpath("hub.sqlite3").read_bytes()


def test_direct_hub_rotation_does_not_inherit_staffing(store):
    store.hub.add_token("cfo")
    store.grant_staffing("cfo")
    replacement = store.hub.add_token("cfo")
    with pytest.raises(HubError, match="staffing authority"):
        store.staffing_node(bearer(replacement))


def test_revoked_identity_cannot_reinherit_staffing(store):
    store.hub.add_token("cfo")
    store.grant_staffing("cfo")
    store.hub.revoke("cfo")
    replacement = store.hub.add_token("cfo")
    with pytest.raises(HubError, match="staffing authority"):
        store.staffing_node(bearer(replacement))


def test_authenticated_old_request_cannot_borrow_replacement_grant(client, app, token, monkeypatch):
    app.state.store.grant_staffing("cfo")
    original = app.state.hub.authenticated_node

    def rotate_after_authentication(request):
        node = original(request)
        app.state.hub.add_token(node)
        app.state.store.grant_staffing(node)
        return node

    monkeypatch.setattr(app.state.hub, "authenticated_node", rotate_after_authentication)
    assert client.post("/v1/workers", json={"action": "spawn"}, headers={"Authorization": "Bearer " + token}).status_code == 403


def test_node_rotation_explicitly_clears_previous_grant(store):
    store.add_node("cfo", staffing=True)
    store.add_node("cfo")
    with store.hub._db() as db:
        grants = db.execute("SELECT COUNT(*) FROM staffing_grants").fetchone()[0]
    assert grants == 0


def test_node_revocation_removes_grant(store):
    store.add_node("cfo", staffing=True)
    store.revoke_node("cfo")
    with store.hub._db() as db:
        grants = db.execute("SELECT COUNT(*) FROM staffing_grants").fetchone()[0]
    assert grants == 0


@pytest.mark.parametrize("name", ["workspace", "Upper", "has space", "", "../escape", None, 1])
@pytest.mark.parametrize("method", ["grant_staffing", "revoke_staffing", "add_node", "revoke_node"])
def test_operator_node_operations_validate_identity(store, method, name):
    with pytest.raises(HubError):
        getattr(store, method)(name)


def test_grant_requires_existing_active_credential(store):
    with pytest.raises(HubError, match="No active node credential"):
        store.grant_staffing("missing")


def test_revoke_staffing_requires_existing_active_credential(store):
    with pytest.raises(HubError, match="No active node credential"):
        store.revoke_staffing("missing")


def test_repeated_grant_is_safe_for_same_credential(store):
    token = store.add_node("cfo", staffing=True)
    store.grant_staffing("cfo")
    assert store.staffing_node(bearer(token)) == "cfo"


def test_repeated_revoke_staffing_keeps_active_credential(store):
    token = store.add_node("cfo", staffing=True)
    store.revoke_staffing("cfo")
    store.revoke_staffing("cfo")
    assert store.hub.node_for(token) == "cfo"


def test_add_requires_boolean_staffing(store):
    with pytest.raises(ValueError, match="boolean"):
        store.add_node("cfo", staffing="yes")


@pytest.fixture
def cli_node(tmp_path):
    path = tmp_path / "node.token"
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path), "node", "add", "cfo",
                                                       "--output", str(path), "--staffing"])
    return tmp_path, path, result


def test_cli_can_add_an_explicit_staffing_delegate(cli_node):
    directory, path, _ = cli_node
    assert ControlStore(directory).staffing_node(bearer(path.read_text().strip())) == "cfo"


def test_cli_delegated_token_file_is_private(cli_node):
    assert stat.S_IMODE(cli_node[1].stat().st_mode) == 0o600


def test_cli_does_not_echo_the_delegated_secret(cli_node):
    assert cli_node[1].read_text().strip() not in cli_node[2].output


@pytest.mark.parametrize("action", ["grant-staffing", "revoke-staffing", "revoke"])
@pytest.mark.parametrize("flag", ["--staffing", "--output"])
def test_cli_refuses_add_only_options_for_other_actions(tmp_path, action, flag):
    option = [flag, str(tmp_path / "unused")] if flag == "--output" else [flag]
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path), "node", action, "cfo", *option])
    assert result.exit_code != 0


def test_cli_grant_existing_node(store):
    token = store.hub.add_token("cfo")
    CliRunner().invoke(build_control_group(), ["--directory", str(store.directory), "node", "grant-staffing", "cfo"])
    assert store.staffing_node(bearer(token)) == "cfo"


def test_cli_revoke_staffing_existing_node(store):
    token = store.add_node("cfo", staffing=True)
    CliRunner().invoke(build_control_group(), ["--directory", str(store.directory), "node", "revoke-staffing", "cfo"])
    with pytest.raises(HubError, match="staffing authority"):
        store.staffing_node(bearer(token))


def test_cli_revoke_invalidates_the_delegate(cli_node):
    directory, path, _ = cli_node
    CliRunner().invoke(build_control_group(), ["--directory", str(directory), "node", "revoke", "cfo"])
    with pytest.raises(HubError, match="revoked"):
        ControlStore(directory).staffing_node(bearer(path.read_text().strip()))


@pytest.mark.parametrize("action", ["grant-staffing", "revoke-staffing"])
def test_cli_unknown_node_authority_operations_fail(tmp_path, action):
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path), "node", action, "missing"])
    assert result.exit_code == 1


@pytest.mark.parametrize("action", ["grant-staffing", "revoke-staffing"])
def test_cli_grant_operations_do_not_echo_node_secret(store, action):
    token = store.add_node("cfo")
    result = CliRunner().invoke(build_control_group(), ["--directory", str(store.directory), "node", action, "cfo"])
    assert token not in result.output


@pytest.mark.parametrize("action", ["grant-staffing", "revoke-staffing"])
def test_cli_rejects_reserved_node_for_authority(store, action):
    result = CliRunner().invoke(build_control_group(), ["--directory", str(store.directory), "node", action, "workspace"])
    assert result.exit_code == 1


def test_cli_rejects_misplaced_option_without_changing_existing_token(store, tmp_path):
    token = store.add_node("cfo", staffing=True)
    CliRunner().invoke(build_control_group(), ["--directory", str(store.directory), "node", "revoke", "cfo", "--staffing"])
    assert store.staffing_node(bearer(token)) == "cfo"


@pytest.mark.parametrize("headers", [[], [(b"authorization", b"Bearer invalid")],
                                      [(b"authorization", b"Basic invalid")]])
def test_staffing_authentication_rejects_missing_or_invalid_bearer(store, headers):
    request = Request({"type": "http", "headers": headers})
    with pytest.raises(HubError, match="bearer|revoked"):
        store.staffing_node(request)


def test_staffing_authentication_rejects_duplicate_headers(store):
    token = store.add_node("cfo", staffing=True)
    value = (b"authorization", ("Bearer " + token).encode())
    request = Request({"type": "http", "headers": [value, value]})
    with pytest.raises(HubError, match="exactly one"):
        store.staffing_node(request)


def test_staffing_grant_fingerprint_matches_current_token(store):
    token = store.add_node("cfo", staffing=True)
    with store.hub._db() as db:
        digest = db.execute("SELECT token_hash FROM staffing_grants WHERE node = 'cfo'").fetchone()[0]
    assert digest == hashlib.sha256(token.encode()).hexdigest()
