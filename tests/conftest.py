"""Shared test isolation: no test may read or write the user's saved provider sessions."""

import pytest


@pytest.fixture(autouse=True)
def isolated_session_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_SESSION_VAULT", str(tmp_path / "session-vault"))
