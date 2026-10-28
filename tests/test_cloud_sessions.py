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
"""Managed session configuration fails before exposing or misrouting credentials."""

import json
from pathlib import Path

import click
from click.testing import CliRunner
import pytest

from one_conv.cloud_cli import cloud_group, session_provider


def test_missing_session_explains_login_gap():
    with pytest.raises(click.ClickException, match="login flow is not implemented"):
        session_provider("chatgpt", None, None)


def test_cowork_makes_no_session_request():
    with pytest.raises(click.ClickException, match="requires browser authentication"):
        session_provider("cowork-cloud", Path("absent"), None)


def test_public_session_file_is_rejected(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(click.ClickException, match="private regular file"):
        session_provider("chatgpt", path, None)


def test_wrong_provider_is_rejected(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"provider": "anthropic"}))
    path.chmod(0o600)
    with pytest.raises(click.ClickException, match="does not match"):
        session_provider("chatgpt", path, None)


@pytest.mark.parametrize("header", ["Host", "Cookie", "Proxy-Authorization"])
def test_unexpected_session_headers_are_rejected(tmp_path, header):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"provider": "openai", "headers": {header: "private"}}))
    path.chmod(0o600)
    with pytest.raises(click.ClickException, match="unsupported headers"):
        session_provider("chatgpt", path, None)


def test_provider_error_uses_cli_error(monkeypatch):
    from one_conv.providers.base import AuthenticationRequired

    def unavailable(*args):
        raise AuthenticationRequired("Reconnect required")

    monkeypatch.setattr("one_conv.cloud_cli.session_provider", unavailable)
    result = CliRunner().invoke(cloud_group, ["sync", "codex-cloud"])
    assert result.output == "Error: Reconnect required\n"
