import pytest


@pytest.fixture(autouse=True)
def no_configured_archive(monkeypatch):
    """Commands run by tests must never sync to the developer's own archive."""
    monkeypatch.delenv("ONE_CONV_ARCHIVE_URL", raising=False)
    monkeypatch.delenv("ONE_CONV_ARCHIVE_TOKEN", raising=False)
    monkeypatch.setenv("ONE_CONV_ARCHIVE_SYNC_MINUTES", "0")
