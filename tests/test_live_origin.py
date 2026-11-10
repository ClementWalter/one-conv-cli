#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""A message sent with no agent session behind it still says where it came from."""

import pytest

from one_conv.live import router

DESKTOP_TREE = {300: (200, "python"), 200: (100, "uv"), 100: (50, "disclaimer"), 50: (1, "Claude")}
SCRIPT_TREE = {300: (200, "python"), 200: (1, "launchd")}


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(router, "_process", DESKTOP_TREE.get)


def test_terminal_sender_is_the_user(desktop):
    assert router.origin(True, pid=300) == "user"


def test_desktop_sender_is_named_with_its_machine(desktop, monkeypatch):
    monkeypatch.setenv("ONE_CONV_HUB_NODE", "macbook")
    assert router.origin(False, pid=300) == "macbook/claude-desktop"


def test_desktop_sender_without_hub_has_no_machine(desktop, monkeypatch, tmp_path):
    monkeypatch.delenv("ONE_CONV_HUB_NODE", raising=False)
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path))
    assert router.origin(False, pid=300) == "claude-desktop"


def test_invalid_machine_name_is_dropped(desktop, monkeypatch):
    monkeypatch.setenv("ONE_CONV_HUB_NODE", "Not A Node")
    assert router.origin(False, pid=300) == "claude-desktop"


def test_other_callers_stay_unknown(monkeypatch):
    monkeypatch.setattr(router, "_process", SCRIPT_TREE.get)
    assert router.origin(False, pid=300) == "unknown"
