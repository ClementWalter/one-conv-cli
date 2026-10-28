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
"""Explicit browser-account selection reuses existing session acquisition."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys

import click
from click.testing import CliRunner
import pytest

from one_conv import cloud_cli
from one_conv.providers.base import AuthenticationRequired, ProviderAccount
from one_conv.providers.chatgpt import ChatGPTProvider


@pytest.fixture
def accounts():
    return [
        {"account_id": "personal", "email": "person@example.test", "session": object()},
        {"account_id": "work", "email": "work@example.test", "session": object()},
    ]


@pytest.mark.parametrize("selector", ["personal", "person@example.test"])
def test_selects_exact_account(selector, accounts):
    provider = cloud_cli.browser_provider("chatgpt", selector, lambda: iter(accounts))
    assert provider.session is accounts[0]["session"]


def test_codex_uses_selected_authenticated_identity(accounts):
    provider = cloud_cli.browser_provider("codex-cloud", "work", lambda: iter(accounts))
    assert provider.account_id == "work"


def test_codex_reuses_selected_session(accounts):
    provider = cloud_cli.browser_provider("codex-cloud", "work", lambda: iter(accounts))
    assert provider.transport.session is accounts[1]["session"]


def test_partial_email_does_not_select_account(accounts):
    with pytest.raises(click.ClickException, match="No signed-in"):
        cloud_cli.browser_provider("chatgpt", "person", lambda: iter(accounts))


def test_duplicate_email_requires_account_id(accounts):
    accounts[1]["email"] = accounts[0]["email"]
    with pytest.raises(click.ClickException, match="Several accounts"):
        cloud_cli.browser_provider("chatgpt", accounts[0]["email"], lambda: iter(accounts))


def test_missing_callback_is_actionable():
    with pytest.raises(click.ClickException, match="unavailable"):
        cloud_cli.browser_provider("chatgpt", "personal", None)


def test_unsupported_provider_never_acquires_accounts():
    def unavailable():
        raise RuntimeError("Account acquisition must not run")
    with pytest.raises(click.ClickException, match="do not yet support"):
        cloud_cli.browser_provider("unknown-provider", "personal", unavailable)


def test_mutually_exclusive_credentials():
    result = CliRunner().invoke(cloud_cli.cloud_group, [
        "sync", "chatgpt", "--browser-account", "personal", "--session-file", "unused.json",
    ])
    assert "mutually exclusive" in result.output


def test_session_environment_conflicts_with_browser_selection(monkeypatch):
    monkeypatch.setenv("ONE_CONV_SESSION_FILE", "unused.json")
    result = CliRunner().invoke(cloud_cli.cloud_group, ["sync", "chatgpt", "--browser-account", "personal"])
    assert "mutually exclusive" in result.output


def test_default_mode_does_not_acquire_browser_accounts(monkeypatch):
    monkeypatch.delenv("ONE_CONV_SESSION_FILE", raising=False)
    def unavailable():
        raise RuntimeError("Implicit account acquisition must not run")
    result = CliRunner().invoke(cloud_cli.cloud_group, ["sync", "chatgpt"], obj={"browser_accounts": unavailable})
    assert "No managed session" in result.output


def test_public_launcher_injects_existing_account_callback(monkeypatch, accounts):
    loader = importlib.machinery.SourceFileLoader(
        "cloud_browser_account_cli", str(Path(__file__).parents[1] / "one_conv_cli.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, loader.name, module)
    loader.exec_module(module)
    monkeypatch.delenv("ONE_CONV_SESSION_FILE", raising=False)
    monkeypatch.setattr(module, "_chatgpt_accounts", lambda: iter(accounts))
    monkeypatch.setattr(cloud_cli, "synchronize", lambda provider, product, limit: {
        "account_id": provider.account_id, "product": product, "limit": limit,
    })
    result = CliRunner().invoke(module.cli, [
        "cloud", "sync", "codex-cloud", "--browser-account", "work", "--limit", "2",
    ])
    assert json.loads(result.output) == {"account_id": "work", "product": "codex-cloud", "limit": 2}


class AccessTransportStub:
    """A session already exchanged for a bearer can still list conversations."""

    def __init__(self, denied=False):
        self.paths = []
        self.denied = denied

    def get(self, path, params=None):
        self.paths.append(path)
        if self.denied:
            raise AuthenticationRequired("Access revoked")
        return {"items": []} if path.endswith("/conversations") else {}


def test_authenticated_account_uses_access_probe_without_cookie_exchange():
    transport = AccessTransportStub()
    provider = ChatGPTProvider(object(), transport=transport,
        authenticated_account=ProviderAccount("personal", "Person", ("list", "read", "search")))
    provider.validate_connection()
    assert transport.paths == ["/backend-api/conversations"]


def test_authenticated_account_preserves_identity_after_access_probe():
    identity = ProviderAccount("personal", "Person", ("list", "read", "search"))
    provider = ChatGPTProvider(object(), transport=AccessTransportStub(), authenticated_account=identity)
    assert provider.validate_connection() == identity


def test_authenticated_identity_does_not_hide_revoked_access():
    provider = ChatGPTProvider(object(), transport=AccessTransportStub(denied=True),
        authenticated_account=ProviderAccount("personal", "Person", ("list",)))
    with pytest.raises(AuthenticationRequired, match="revoked"):
        provider.validate_connection()


def test_browser_selection_passes_verified_identity(accounts):
    provider = cloud_cli.browser_provider("chatgpt", "personal", lambda: iter(accounts))
    assert provider.authenticated_account.id == "personal"
