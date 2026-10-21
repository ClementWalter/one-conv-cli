"""Shared test isolation: no test may read or write the user's saved provider sessions or archive."""

import pytest


@pytest.fixture(autouse=True)
def isolated_session_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_SESSION_VAULT", str(tmp_path / "session-vault"))
    monkeypatch.setenv("ONE_CONV_LOGIN_PROFILES", str(tmp_path / "login-profiles"))
    # Live relay state must never reach the user's real inboxes or registries.
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live-relay"))


@pytest.fixture(autouse=True)
def no_configured_archive(monkeypatch):
    """Commands run by tests must never sync to the developer's own archive."""
    monkeypatch.delenv("ONE_CONV_ARCHIVE_URL", raising=False)
    monkeypatch.delenv("ONE_CONV_ARCHIVE_TOKEN", raising=False)
    monkeypatch.delenv("ONE_CONV_ARCHIVE_ORIGIN", raising=False)
    monkeypatch.setenv("ONE_CONV_ARCHIVE_SYNC_MINUTES", "0")
