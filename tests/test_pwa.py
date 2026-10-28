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
"""Public installation assets use the production HTTP and browser boundaries."""

import json
import struct

import pytest

from one_conv.live import pwa
from tests import test_live_web as web_tests

fetch = web_tests.fetch
homes = web_tests.homes
viewer = web_tests.viewer


@pytest.mark.parametrize("path", list(pwa.ASSETS))
def test_install_asset_is_served(viewer, path):
    assert fetch(viewer, path)[0] == 200


@pytest.mark.parametrize("path", list(pwa.ASSETS))
def test_install_asset_refuses_a_foreign_host(viewer, path):
    assert fetch(viewer, path, host="foreign.example")[0] == 421


@pytest.mark.parametrize("path,size", [
    ("/icons/icon-192.png", 192), ("/icons/icon-512.png", 512), ("/icons/apple-touch-icon.png", 180),
])
def test_icon_dimensions_match_the_installation_metadata(path, size):
    assert struct.unpack(">II", pwa.ASSETS[path][0][16:24]) == (size, size)


def test_manifest_opens_as_a_standalone_app(viewer):
    assert json.loads(fetch(viewer, "/manifest.webmanifest")[2])["display"] == "standalone"


def test_manifest_uses_the_workspace_root(viewer):
    assert json.loads(fetch(viewer, "/manifest.webmanifest")[2])["start_url"] == "/"


def test_browser_can_load_the_worker_under_the_content_policy(viewer):
    assert "worker-src 'self'" in fetch(viewer, "/")[1]["content-security-policy"]


def test_public_shell_contains_no_user_message_log(viewer):
    assert "messages.jsonl" not in fetch(viewer, "/offline.html")[2].decode()
