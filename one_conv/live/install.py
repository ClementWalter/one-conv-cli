"""Install the per-harness pieces live relaying needs, each one checked before it is written.

Claude Code and Codex register the one-conv MCP server through their own CLIs; omp gets an
extension file; Cursor gets its MCP entry plus a `stop` hook. Claude Code also needs
`crossSessionInbound: "accept"`, without which bypass-mode sessions hold relayed messages.
"""

import json
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import claude, cursor, omp


@dataclass
class Step:
    harness: str
    summary: str
    done: Callable[[], bool]
    apply: Callable[[], None]


def _json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".one-conv.tmp")
    partial.write_text(json.dumps(data, indent=2) + "\n")
    if path.exists():
        partial.chmod(path.stat().st_mode)
    partial.rename(path)


def _registered(command: list[str], mcp: list[str]) -> bool:
    """The server exists and its printed command line is the one this checkout would register."""
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False
    return result.returncode == 0 and mcp[0] in result.stdout and " ".join(mcp[1:]) in result.stdout


def _replace(remove: list[str], add: list[str]) -> None:
    subprocess.run(remove, check=False, capture_output=True, text=True, timeout=60)
    _run(add)


def _run(command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"{shlex.join(command)} failed: {result.stderr.strip() or result.stdout.strip()}")


def plan(launcher: list[str]) -> list[Step]:
    """`launcher` is the argv prefix that runs this one-conv checkout."""
    mcp = [*launcher, "mcp"]
    settings = claude.claude_home() / "settings.json"
    cursor_mcp = cursor.cursor_home() / "mcp.json"
    cursor_hooks = cursor.cursor_home() / "hooks.json"
    hook_command = shlex.join([*launcher, "live", "cursor-hook"])
    extension = omp.render_extension(launcher)

    def inbound_done():
        return _json_file(settings).get("crossSessionInbound") == "accept"

    def inbound_apply():
        data = _json_file(settings)
        data["crossSessionInbound"] = "accept"
        _write_json(settings, data)

    def extension_done():
        path = omp.extension_path()
        return path.exists() and path.read_text() == extension

    def extension_apply():
        omp.extension_path().parent.mkdir(parents=True, exist_ok=True)
        omp.extension_path().write_text(extension)

    def cursor_mcp_done():
        return _json_file(cursor_mcp).get("mcpServers", {}).get("one-conv") == {"command": mcp[0], "args": mcp[1:]}

    def cursor_mcp_apply():
        data = _json_file(cursor_mcp)
        data.setdefault("mcpServers", {})["one-conv"] = {"command": mcp[0], "args": mcp[1:]}
        _write_json(cursor_mcp, data)

    def hook_entries():
        return _json_file(cursor_hooks).get("hooks", {}).get("stop", [])

    def cursor_hook_done():
        return any(entry.get("command") == hook_command for entry in hook_entries())

    def cursor_hook_apply():
        data = _json_file(cursor_hooks)
        data.setdefault("version", 1)
        stops = data.setdefault("hooks", {}).setdefault("stop", [])
        # A reinstall from another checkout path replaces the old relay hook instead of adding a second.
        stops[:] = [entry for entry in stops if "live cursor-hook" not in entry.get("command", "")]
        stops.append({"command": hook_command})
        _write_json(cursor_hooks, data)

    return [
        Step("claude", "register the one-conv MCP server (user scope)",
             lambda: _registered(["claude", "mcp", "get", "one-conv"], mcp),
             lambda: _replace(["claude", "mcp", "remove", "--scope", "user", "one-conv"],
                              ["claude", "mcp", "add", "--scope", "user", "one-conv", "--", *mcp])),
        Step("claude", f'set "crossSessionInbound": "accept" in {settings}; any local process running as '
             "you can then inject user turns into every Claude Code session, including bypass ones",
             inbound_done, inbound_apply),
        Step("codex", "register the one-conv MCP server",
             lambda: _registered(["codex", "mcp", "get", "one-conv"], mcp),
             lambda: _replace(["codex", "mcp", "remove", "one-conv"],
                              ["codex", "mcp", "add", "one-conv", "--", *mcp])),
        Step("omp", f"write the relay extension {omp.extension_path()}", extension_done, extension_apply),
        Step("cursor", f"add the one-conv MCP server to {cursor_mcp}", cursor_mcp_done, cursor_mcp_apply),
        Step("cursor", f"add the relay stop hook to {cursor_hooks}", cursor_hook_done, cursor_hook_apply),
    ]
