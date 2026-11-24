"""Start an agent in a detached tmux session, briefed and reachable over one-conv.

The harness runs its own interactive TUI, so it registers like any session the user starts
and stays attachable. Its first prompt is the brief, the one-conv MCP tools are pre-approved
through the harness's per-launch flags (Cursor reads them from the allowlist `live install`
writes), and its working directory is trusted. Shell and edit approvals stay as the user set them.
"""

import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import agy
from .model import LiveSession

SPAWNABLE = ("claude", "codex", "cursor", "omp", "agy")
# The tools an agent needs to report, ask and follow its peers; the cloud account tools stay gated.
TOOLS = ("send_message", "set_name", "list_live_sessions", "read_conversation", "search_conversations",
         "find_conversations")


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]


def launch(harness: str, brief: str, cwd: str, extra: list[str]) -> Launch:
    """The harness command that opens an interactive session on `brief`."""
    if harness == "claude":
        allowed = ",".join(f"mcp__one-conv__{tool}" for tool in TOOLS)
        return Launch(["claude", *extra, "--allowedTools", allowed, "--", brief], {})
    if harness == "codex":
        approvals = [part for tool in TOOLS
                     for part in ("-c", f'mcp_servers.one-conv.tools.{tool}.approval_mode="approve"')]
        return Launch(["codex", *extra, *approvals, "-c", f'projects={{"{cwd}"={{trust_level="trusted"}}}}', brief], {})
    if harness == "cursor":
        return Launch(["agent", *extra, "--trust", brief], {})
    if harness == "omp":
        return Launch(["omp", *extra, brief], {})
    if harness == "agy":
        argv, env = agy.launch_command([*extra, "--prompt-interactive", brief])
        return Launch(argv, {agy.TOKEN_ENV: env[agy.TOKEN_ENV]})
    raise ValueError(f"cannot spawn {harness!r}")


def with_reply_path(brief: str, sender: LiveSession | None) -> str:
    """The brief, headed by who started the agent and where its reports go."""
    if sender is None or sender.id.startswith("?"):
        return brief
    return (f"You were started by {sender.address} with `one-conv live spawn`. Send your reports and "
            f"questions with the one-conv `send_message` tool to `{sender.address}`.\n\n{brief}")


def tmux_name(harness: str, cwd: str, taken: Callable[[str], bool]) -> str:
    """`<harness>-<folder>`, numbered when taken; tmux refuses `.` and `:` in names."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{harness}-{Path(cwd).name}").strip("-")[:48]
    name, suffix = base, 2
    while taken(name):
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], check=False, capture_output=True, text=True, timeout=30)


def tmux_taken(name: str) -> bool:
    return _tmux("has-session", "-t", f"={name}").returncode == 0


def start(name: str, cwd: str, command: Launch) -> None:
    env_flags = [part for key, value in command.env.items() for part in ("-e", f"{key}={value}")]
    # Several arguments after the options make tmux exec the command without a shell, so the brief needs no quoting.
    result = _tmux("new-session", "-d", "-s", name, "-c", cwd, *env_flags, "--", *command.argv)
    if result.returncode:
        raise RuntimeError(f"tmux could not start {shlex.join(command.argv[:1])}: {result.stderr.strip()}")


def screen(name: str, lines: int = 15) -> str:
    output = _tmux("capture-pane", "-p", "-t", f"={name}:").stdout
    return "\n".join([line for line in output.splitlines() if line.strip()][-lines:])


def _same_dir(left: str, right: str) -> bool:
    return bool(left) and os.path.realpath(left) == os.path.realpath(right)


def wait_for(list_sessions: Callable[[], list[LiveSession]], before: set[str], cwd: str, timeout: float,
             alive: Callable[[], bool], poll: float = 1.0, sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> LiveSession | None:
    """The first session that was not running before and works in `cwd`; None at timeout or once the pane is gone."""
    deadline = clock() + timeout
    while True:
        for session in list_sessions():
            if session.id not in before and _same_dir(session.cwd, cwd):
                return session
        if clock() >= deadline or not alive():
            return None
        sleep(poll)
