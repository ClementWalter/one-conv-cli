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
#     "numpy==2.5.4",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""The Desktop extension's tools (mcpb.json `tools`) track the CLI's real command tree.

Every visible CLI command is either shipped as an extension tool or excluded below with
the reason it cannot work there; an entry naming a command that no longer exists fails too.
"""

import json
from pathlib import Path

import click
import pytest

from one_conv_cli import cli

ROOT = Path(__file__).resolve().parents[1]
TOOLS = json.loads((ROOT / "mcpb.json").read_text())["tools"]

# Commands the extension does not ship, each with why.
EXCLUDED = {
    "mcp": "The MCP stdio server itself; the extension already is one (mcp-bridge never serves `mcp`).",
    "live serve": "The long-running relay bridge; a tool call must return (mcp-bridge never serves `serve`).",
    "live agy": "Starts agy's interactive terminal UI, which needs a terminal.",
    "live join": "Installs the bridge as a login service run from this copy, which every extension update "
                 "replaces; join from a CLI checkout instead.",
    "cloud chat": "Alias of `cloud read`.",
    "chats": "Legacy alias of `local chats`.",
    "read": "Legacy alias of `local read`.",
    "thread": "Legacy alias of `local thread`.",
    "search": "Legacy alias of `local search`.",
    "unread": "Legacy alias of `local unread`.",
    "find": "Legacy alias of `local find`.",
    "skill-usage": "Legacy alias of `local skill-usage`.",
    "fork": "Legacy alias of `local fork`.",
    "send": "Legacy alias of `local send`.",
    "port": "Legacy alias of `local port`.",
}

# Path words mcp-bridge never serves (its EXCLUDED set): a tool naming one fails the pack.
BRIDGE_SKIPPED = frozenset({"login", "logout", "auth", "pair", "listen", "serve", "watch", "poll", "mcp"})


def leaves(command: click.Command, path: tuple[str, ...] = ()):
    """(path, command) for every leaf, walked like mcp-bridge's introspect.py walks it."""
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            yield from leaves(sub, (*path, name))
        return
    yield " ".join(path), command


# Hidden commands are hooks and internals; mcp-bridge never serves them.
COMMANDS = sorted(path for path, command in leaves(cli) if not command.hidden)


@pytest.mark.parametrize("path", COMMANDS)
def test_every_command_is_shipped_or_excluded(path):
    assert path in TOOLS or path in EXCLUDED, (
        f"`{path}` is new: add it to mcpb.json `tools`, or to EXCLUDED here with the reason it cannot run there")


@pytest.mark.parametrize("tool", TOOLS)
def test_every_tool_names_a_command(tool):
    assert tool in COMMANDS


@pytest.mark.parametrize("path", sorted(EXCLUDED))
def test_every_exclusion_names_a_command(path):
    assert path in COMMANDS


@pytest.mark.parametrize("path", sorted(EXCLUDED))
def test_no_exclusion_is_shipped(path):
    assert path not in TOOLS


@pytest.mark.parametrize("tool", TOOLS)
def test_no_tool_uses_a_word_the_bridge_skips(tool):
    assert not BRIDGE_SKIPPED & set(tool.split())


def test_tools_are_listed_once():
    assert len(TOOLS) == len(set(TOOLS))
