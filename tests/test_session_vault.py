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
"""Saved provider sessions survive browser account switches and stay private."""

import importlib.machinery
import importlib.util
import stat
import sys
from pathlib import Path

import pytest

from one_conv import browser_auth
from one_conv.session_vault import SessionVault


@pytest.fixture
def vault(tmp_path):
    return SessionVault(tmp_path / "vault")


def test_round_trip(vault):
    vault.save("claude", "id-1", "chrome/Default", {"sessionKey": "k1"})
    assert vault.entries("claude")[0].cookies == {"sessionKey": "k1"}


def test_same_identity_overwrites(vault):
    vault.save("claude", "id-1", "chrome/Default", {"sessionKey": "old"})
    vault.save("claude", "id-1", "chrome/Default", {"sessionKey": "new"})
    assert [entry.cookies["sessionKey"] for entry in vault.entries("claude")] == ["new"]


def test_distinct_identities_are_kept(vault):
    vault.save("claude", "zama", "chrome/Default", {"sessionKey": "z"})
    vault.save("claude", "perso", "chrome/Default", {"sessionKey": "p"})
    assert len(vault.entries("claude")) == 2


def test_providers_are_separate(vault):
    vault.save("chatgpt", "acct", "chrome/Default", {"t": "v"})
    assert vault.entries("claude") == []


def test_files_are_private(vault):
    vault.save("claude", "id-1", "chrome/Default", {"sessionKey": "k1"})
    path = next(vault.root.glob("claude-*.json"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_directory_is_private(vault):
    vault.save("claude", "id-1", "chrome/Default", {"sessionKey": "k1"})
    assert stat.S_IMODE(vault.root.stat().st_mode) == 0o700


def test_readable_file_is_ignored(vault):
    vault.save("claude", "id-1", "chrome/Default", {"sessionKey": "k1"})
    next(vault.root.glob("claude-*.json")).chmod(0o644)
    assert vault.entries("claude") == []


def test_unknown_provider_is_refused(vault):
    with pytest.raises(ValueError):
        vault.save("other", "id", "label", {})


def test_missing_vault_has_no_entries(tmp_path):
    assert SessionVault(tmp_path / "absent").entries("claude") == []


class StubSession:
    """Answer the organizations route per session key, like separate claude.ai logins."""

    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def get(self, url, **kwargs):
        response = type("Response", (), {"status_code": self.status, "headers": {}})()
        response.json = lambda: self.payload
        return response


ORGS = {"perso-key": [{"uuid": "perso-org", "name": "Personal"}],
        "zama-key": [{"uuid": "zama-org", "name": "Zama"}]}


def _factory(cookies):
    key = cookies["sessionKey"]
    return StubSession(ORGS[key]) if key in ORGS else StubSession({}, status=401)


def test_on_verified_receives_organizations():
    seen = []
    list(browser_auth.claude_accounts(lambda: [("p", {"sessionKey": "zama-key"})], session_factory=_factory,
                                      on_verified=lambda *args: seen.append(args)))
    assert seen == [("p", {"sessionKey": "zama-key"}, ORGS["zama-key"])]


def test_on_verified_skips_expired_session():
    seen = []
    list(browser_auth.claude_accounts(lambda: [("p", {"sessionKey": "gone"})], session_factory=_factory,
                                      on_verified=lambda *args: seen.append(args)))
    assert seen == []


@pytest.fixture
def app(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("vault_cli", str(Path(__file__).parents[1] / "one_conv_cli.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    monkeypatch.setattr(browser_auth, "_claude_session", _factory)
    return module


def _organizations(app):
    return sorted(account["organization_id"] for account in app._claude_accounts())


def test_saved_login_is_listed_after_browser_switch(app, monkeypatch):
    monkeypatch.setattr(app, "_chatgpt_cookie_jars", lambda *a: [("chrome/Default", {"sessionKey": "zama-key"})])
    _organizations(app)
    monkeypatch.setattr(app, "_chatgpt_cookie_jars", lambda *a: [("chrome/Default", {"sessionKey": "perso-key"})])
    assert _organizations(app) == ["perso-org", "zama-org"]


def test_saved_login_keeps_its_account_id(app, monkeypatch):
    monkeypatch.setattr(app, "_chatgpt_cookie_jars", lambda *a: [("chrome/Default", {"sessionKey": "zama-key"})])
    before = [account["account_id"] for account in app._claude_accounts()]
    monkeypatch.setattr(app, "_chatgpt_cookie_jars", lambda *a: [("chrome/Default", {"sessionKey": "perso-key"})])
    after = [a["account_id"] for a in app._claude_accounts() if a["organization_id"] == "zama-org"]
    assert after == before


def test_expired_saved_login_is_dropped(app, monkeypatch):
    app._session_vault().save("claude", "chrome/Default:old-org", "chrome/Default", {"sessionKey": "gone"})
    monkeypatch.setattr(app, "_chatgpt_cookie_jars", lambda *a: [("chrome/Default", {"sessionKey": "perso-key"})])
    assert _organizations(app) == ["perso-org"]


class FakeResponse:
    def __init__(self, cookies):
        self.cookies = cookies


def test_rotated_chunked_token_replaces_saved_one(app):
    response = FakeResponse({"__Secure-next-auth.session-token.0": "ab", "__Secure-next-auth.session-token.1": "cd"})
    jar = app._chatgpt_rotated_jar({"__Secure-next-auth.session-token": "old", "other": "x"}, response)
    assert jar["__Secure-next-auth.session-token"] == "abcd"


def test_jar_without_rotation_is_unchanged(app):
    jar = {"__Secure-next-auth.session-token": "old"}
    assert app._chatgpt_rotated_jar(jar, FakeResponse({})) == jar


class RefreshFailedSession:
    def __init__(self):
        self.headers = {}

    def get(self, url, **kwargs):
        response = FakeResponse({})
        response.json = lambda: {"accessToken": "t", "error": "RefreshAccessTokenError", "account": {"id": "a"}}
        return response


def test_refresh_error_counts_as_signed_out(app, monkeypatch):
    monkeypatch.setattr(app, "_chatgpt_client", lambda jar: RefreshFailedSession())
    assert app._chatgpt_authenticate({"__Secure-next-auth.session-token": "stale"}) is None


def test_revoked_saved_login_is_removed(app, monkeypatch):
    app._session_vault().save("claude", "chrome/Default:old-org", "chrome/Default", {"sessionKey": "gone"})
    monkeypatch.setattr(app, "_chatgpt_cookie_jars", lambda *a: [("chrome/Default", {"sessionKey": "perso-key"})])
    _organizations(app)
    assert [entry.identity for entry in app._session_vault().entries("claude")] == ["chrome/Default:perso-org"]


def test_saved_login_records_organization_names(app, monkeypatch):
    monkeypatch.setattr(app, "_chatgpt_cookie_jars", lambda *a: [("chrome/Default", {"sessionKey": "zama-key"})])
    _organizations(app)
    assert app._session_vault().entries("claude")[0].description == "Zama"


def test_remove_deletes_entry(vault):
    vault.save("claude", "id-1", "chrome/Default", {"sessionKey": "k1"})
    vault.remove("claude", "id-1")
    assert vault.entries("claude") == []
