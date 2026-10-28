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
#     "numpy==2.4.6",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Shared test isolation: no test may read or write the user's saved provider sessions or archive."""

import pytest


@pytest.fixture(autouse=True)
def isolated_session_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_SESSION_VAULT", str(tmp_path / "session-vault"))
    monkeypatch.setenv("ONE_CONV_LOGIN_PROFILES", str(tmp_path / "login-profiles"))
    # Live relay state must never reach the user's real inboxes or registries.
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live-relay"))
    # Caches derived from history must not land in the user's state directory either.
    monkeypatch.setenv("ONE_CONV_SEARCH_INDEX", str(tmp_path / "search-index.sqlite"))


@pytest.fixture(autouse=True)
def no_configured_archive(monkeypatch):
    """Commands run by tests must never sync to the developer's own archive."""
    monkeypatch.delenv("ONE_CONV_ARCHIVE_URL", raising=False)
    monkeypatch.delenv("ONE_CONV_ARCHIVE_TOKEN", raising=False)
    monkeypatch.delenv("ONE_CONV_ARCHIVE_ORIGIN", raising=False)
    monkeypatch.setenv("ONE_CONV_ARCHIVE_SYNC_MINUTES", "0")
