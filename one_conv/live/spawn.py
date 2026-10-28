"""Start a coding agent detached in tmux, so it stays resident and reachable by `live send`.

A headless run (`codex exec`, `claude -p`) exits after its turn, so a message queued for it
is never read. An interactive harness in a detached tmux session keeps running between
turns, registers like any open session, and can be watched with `tmux attach` when wanted.
"""

import json
import logging
import shlex
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import codex, cursor, omp, router
from .model import LiveSession, live_home

logger = logging.getLogger(__name__)
HARNESSES = ("claude", "codex", "omp", "cursor")
# Cursor's terminal agent is the `agent` command; every other harness runs under its own name.
BINARIES = {"cursor": "agent"}
# Each harness's interactive command with a first prompt; the prompt is one argv element.
COMMANDS = {
    "claude": lambda prompt, model: ["claude", *(["--model", model] if model else []), prompt],
    "codex": lambda prompt, model: ["codex", *(["--model", model] if model else []), prompt],
    "omp": lambda prompt, model: ["omp", *([f"--model={model}"] if model else []), prompt],
    "cursor": lambda prompt, model: ["agent", *(["--model", model] if model else []), prompt],
}
SESSION_PREFIX = "oneconv-"
# Harness credentials handed to every spawned pane.
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CURSOR_API_KEY")


def environment(env_file: str | None = None) -> dict[str, str]:
    """What a spawned pane gets on top of the tmux server's environment: this process's harness
    credentials, then every variable `env_file` sets.

    The file is read at each spawn, so a token written or rotated after the bridge started is used.
    It holds `KEY=value` lines, optionally prefixed by `export` and shell-quoted.
    """
    found = {var: os.environ[var] for var in AUTH_ENV if var in os.environ}
    if not env_file:
        return found
    try:
        text = Path(env_file).read_text()
    except OSError as error:
        raise SpawnError(f"cannot read the spawner's env file {env_file}: {error.strerror or error}") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        if separator and key.strip().isidentifier():
            try:
                parts = shlex.split(value)
            except ValueError:
                continue
            found[key.strip()] = parts[0] if parts else ""
    return found


class SpawnError(Exception):
    """A spawn that cannot start, or a started agent that never registered."""


class NotSpawned(SpawnError):
    """No agent the spawner started matches; the session, if any, runs outside its tmux sessions."""


def spawner_env_file() -> str | None:
    """The env file this machine's spawner settings name, so any spawn here gets the credentials hub spawns get."""
    try:
        data = json.loads((live_home() / "bridge" / "spawn.json").read_text())
    except (OSError, ValueError):
        return None
    return data.get("env_file") if isinstance(data, dict) and isinstance(data.get("env_file"), str) else None


# Claude Code versions seen opening no messaging socket: their sessions run but no message reaches them.
DEAF = "deaf.json"
# How long a registered Claude Code session gets to open its messaging socket.
SOCKET_WAIT_SECONDS = 10.0


def _deaf_path() -> Path:
    return live_home() / "bridge" / DEAF


def claude_version() -> str:
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def _deaf_versions() -> list[str]:
    try:
        rows = json.loads(_deaf_path().read_text())
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def _record_deaf(version: str) -> None:
    path = _deaf_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted({*_deaf_versions(), version})))


@dataclass
class Spawned:
    tmux: str
    harness: str
    cwd: str
    address: str = ""

    def to_json(self):
        return {"tmux": self.tmux, "harness": self.harness, "cwd": self.cwd, "address": self.address,
                "attach": f"tmux attach -t {self.tmux}"}


# The bridge's idle sweep and a spawn running on another thread both rewrite the registry.
_registry_lock = threading.RLock()


def _registry() -> Path:
    return live_home() / "spawned.json"


def _load() -> dict:
    try:
        rows = json.loads(_registry().read_text())
    except (OSError, ValueError):
        return {}
    return rows if isinstance(rows, dict) else {}


def _save(rows: dict) -> None:
    path = _registry()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(rows, indent=1))
    os.replace(partial, path)


def command(harness: str, prompt: str, model: str | None, extra: tuple[str, ...],
            trust_folder: bool = False) -> list[str]:
    """The argv tmux runs directly, without a shell, so the prompt stays literal."""
    if harness not in COMMANDS:
        raise SpawnError(f"cannot spawn {harness!r}; choose one of {', '.join(HARNESSES)}")
    if not prompt.strip():
        raise SpawnError("a spawned agent needs a first prompt")
    argv = COMMANDS[harness](prompt, model)
    # Cursor takes trust as a flag; Claude Code's trust question is answered in the pane.
    trust = ("--trust",) if harness == "cursor" and trust_folder else ()
    return [argv[0], *trust, *extra, *argv[1:]]


def preflight(harness: str, env: dict[str, str] | None = None) -> None:
    """Refuse a spawn that could not end up reachable, before anything starts."""
    if shutil.which("tmux") is None:
        raise SpawnError("live spawn needs tmux on PATH")
    binary = BINARIES.get(harness, harness)
    if shutil.which(binary) is None:
        raise SpawnError(f"{binary} is not on PATH")
    if harness == "claude" and subprocess.run(["claude", "auth", "status"], capture_output=True, check=False,
                                              env={**os.environ, **(env or {})}).returncode:
        # A logged-out Claude Code registers, then answers every prompt with "Not logged in".
        raise SpawnError("claude is not logged in on this machine")
    if harness == "claude" and _deaf_versions() and (version := claude_version()) in _deaf_versions():
        raise SpawnError(f"Claude Code {version} opens no messaging socket, so no message could reach its"
                         " sessions; update Claude Code on this machine")
    if harness == "omp" and not omp.extension_path().exists():
        # Without the extension an omp session never registers, so nothing could message it.
        raise SpawnError("omp sessions register only with the live extension; run `one-conv live install --yes` first")


def start(harness: str, prompt: str, cwd: Path, model: str | None = None, extra: tuple[str, ...] = (),
          wait_seconds: float = 60.0, trust_folder: bool = False, env: dict[str, str] | None = None) -> Spawned:
    """Start the agent detached in tmux, then wait until it is listed as a live session.

    `trust_folder` answers Claude Code's first-visit folder-trust question with yes, and passes
    `--trust` to Cursor; without it, a spawn into an untrusted folder stops at that question.
    """
    preflight(harness, env)
    name = f"{SESSION_PREFIX}{harness}-{uuid.uuid4().hex[:8]}"
    if harness == "codex":
        # Codex threads carry no pid, so the spawn name in the first prompt is what identifies this one.
        prompt = f"{prompt}\n\n(one-conv spawn {name})"
    argv = command(harness, prompt, model, extra, trust_folder)
    before = {s.address for s in router.list_all(harness)}
    # A tmux server keeps the environment it started with, which may predate the credentials this process has.
    variables = environment(spawner_env_file()) if env is None else env
    credentials = [flag for key, value in variables.items() for flag in ("-e", f"{key}={value}")]
    result = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", str(cwd), "-x", "200", "-y", "50",
                             *credentials, "--", *argv], capture_output=True, text=True, check=False)
    if result.returncode:
        raise SpawnError(f"tmux could not start {harness}: {result.stderr.strip() or result.returncode}")
    spawned = Spawned(name, harness, str(cwd))
    with _registry_lock:
        rows = _load()
        rows[name] = spawned.to_json()
        _save(rows)
    spawned.address = wait_for_session(harness, cwd, before, name, wait_seconds, trust_folder,
                                       accept_bypass=BYPASS_FLAG in extra)
    if harness == "claude" and not _socket_opened(spawned.address):
        stop(name)
        version = claude_version()
        _record_deaf(version)
        raise SpawnError(f"Claude Code {version} started {spawned.address} without a messaging socket, so no"
                         " message could reach it; update Claude Code on this machine")
    with _registry_lock:
        rows = _load()
        rows[name] = spawned.to_json()
        _save(rows)
    return spawned


def _socket_opened(address: str) -> bool:
    """Whether the registered Claude Code session opened the socket messages are delivered through."""
    deadline = time.monotonic() + SOCKET_WAIT_SECONDS
    while True:
        if any(s.address == address and s.delivery == "socket" for s in router.list_all("claude")):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def pane_for(address: str) -> str | None:
    """The tmux session of the spawned agent at `address`, while it still runs."""
    return next((name for name, row in _load().items() if row.get("address") == address and _alive(name)), None)


def type_into(tmux_name: str, text: str) -> None:
    """Submit `text` in the pane as one bracketed paste, so its newlines do not submit it early."""
    buffer = f"{tmux_name}-{uuid.uuid4().hex[:8]}"
    for argv, stdin in ((["tmux", "load-buffer", "-b", buffer, "-"], text),
                        (["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", tmux_name], None),
                        (["tmux", "send-keys", "-t", tmux_name, "Enter"], None)):
        result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
        if result.returncode:
            raise SpawnError(f"could not type into tmux session {tmux_name}: {result.stderr.strip() or result.returncode}")


# What tmux says when the session, or its whole server, is gone; any other failure proves nothing.
ABSENT = ("can't find session", "no server running on")


def _state(tmux_name: str) -> str:
    """alive, absent, or unknown when tmux could not be asked (a connection or permission error)."""
    result = subprocess.run(["tmux", "has-session", "-t", tmux_name], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return "alive"
    return "absent" if any(phrase in (result.stderr or "") for phrase in ABSENT) else "unknown"


def _alive(tmux_name: str) -> bool:
    """False only when tmux reports the session gone, so an unreachable server never drops an agent."""
    return _state(tmux_name) != "absent"


# Claude Code's first-visit question for a folder; its default answer is "No, exit".
TRUST_QUESTION = "Is this a project you created or one you trust?"
# Claude Code's one-time consent to --dangerously-skip-permissions on a machine; its default is also "No, exit".
BYPASS_QUESTION = "running in Bypass Permissions mode"
BYPASS_FLAG = "--dangerously-skip-permissions"


def _pane_pid(tmux_name: str) -> int | None:
    result = subprocess.run(["tmux", "display-message", "-p", "-t", tmux_name, "#{pane_pid}"], capture_output=True,
                            text=True, check=False)
    return int(result.stdout) if result.returncode == 0 and result.stdout.strip().isdigit() else None


def _process_tree(root: int) -> set[int]:
    """`root` and every process descended from it."""
    result = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=False)
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    tree, pending = set(), [root]
    while pending:
        pid = pending.pop()
        if pid not in tree:
            tree.add(pid)
            pending.extend(children.get(pid, ()))
    return tree


def _codex_rollout_mentions(thread_id: str, marker: str) -> bool:
    for path in (codex.codex_home() / "sessions").rglob(f"rollout-*{thread_id}.jsonl"):
        try:
            with path.open() as handle:
                if any(marker in line for _, line in zip(range(200), handle)):
                    return True
        except OSError:
            continue
    return False


def owned_by_spawn(session: LiveSession, tmux_name: str) -> bool:
    """Whether `session` is the agent tmux started, not another one that registered meanwhile."""
    if session.harness == "codex":
        return _codex_rollout_mentions(session.id, f"(one-conv spawn {tmux_name})")
    root = _pane_pid(tmux_name)
    return bool(session.pid and root and session.pid in _process_tree(root))


def _cursor_chat(tmux_name: str) -> str | None:
    """The chat the pane's `agent` process holds open; it exists from the first prompt, before any hook runs."""
    root = _pane_pid(tmux_name)
    return next((chat for pid in sorted(_process_tree(root)) if (chat := cursor.chat_for_pid(pid))), None) \
        if root else None


def screen(tmux_name: str) -> str:
    """The visible text of the pane."""
    return subprocess.run(["tmux", "capture-pane", "-p", "-t", tmux_name], capture_output=True, text=True,
                          check=False).stdout


def _choose_second(tmux_name: str) -> None:
    """Move from a Claude Code dialog's default "No, exit" to its "Yes" answer, then confirm."""
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Down"], check=False)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=False)


def wait_for_session(harness: str, cwd: Path, before: set[str], tmux_name: str, wait_seconds: float,
                     trust_folder: bool = False, accept_bypass: bool = False) -> str:
    """The address of the session started in tmux session `tmux_name`; refuses if it never registers.

    `accept_bypass` confirms the bypass-permissions consent a machine shows once, for a spawn
    that was explicitly given --dangerously-skip-permissions.
    """
    deadline = time.monotonic() + wait_seconds
    resolved = str(cwd.resolve())
    answered = consented = False
    while time.monotonic() < deadline:
        if not _alive(tmux_name):
            raise SpawnError(f"{harness} exited before registering; inspect it with a manual run in {cwd}")
        if harness == "claude" and not answered and TRUST_QUESTION in screen(tmux_name):
            if not trust_folder:
                raise SpawnError(f"Claude Code is asking whether to trust {cwd}. Answer it with "
                                 f"`tmux attach -t {tmux_name}`, or spawn again with --trust-folder.")
            _choose_second(tmux_name)
            answered = True
        elif harness == "claude" and accept_bypass and not consented and BYPASS_QUESTION in screen(tmux_name):
            _choose_second(tmux_name)
            consented = True
        if harness == "cursor":
            # A Cursor chat registers only when its first turn ends; `live send` already reaches it by id.
            if chat := _cursor_chat(tmux_name):
                return f"cursor:{chat}"
            time.sleep(1)
            continue
        fresh = [s for s in router.list_all(harness) if s.address not in before and _same_dir(s, resolved)
                 and owned_by_spawn(s, tmux_name)]
        if len(fresh) == 1:
            return fresh[0].address
        time.sleep(1)
    raise SpawnError(f"{harness} started in tmux session {tmux_name} but did not register within "
                     f"{wait_seconds:.0f}s; attach with `tmux attach -t {tmux_name}` to see why")


def _same_dir(session: LiveSession, resolved: str) -> bool:
    try:
        return str(Path(session.cwd).resolve()) == resolved
    except OSError:
        return False


def listing() -> list[dict]:
    """Spawned agents whose tmux session is still running; finished ones are dropped."""
    with _registry_lock:
        rows = _load()
        alive = {name: row for name, row in rows.items() if _alive(name)}
        if alive.keys() != rows.keys():
            _save(alive)
    return list(alive.values())


# A spawned agent idle this long is ended; a new post in its channel starts a fresh one that catches up.
IDLE_HOURS = float(os.environ.get("ONE_CONV_SPAWN_IDLE_HOURS", "24"))


def retire_idle(sessions: list[LiveSession], now: float | None = None, idle_hours: float = IDLE_HOURS) -> list[dict]:
    """End spawned agents seen idle for `idle_hours`; returns the rows of those ended.

    Each row keeps `active_at`, the last time its session was seen doing anything but waiting
    for a message (busy, or waiting on the human), so a bridge restart never resets the clock.
    """
    now = time.time() if now is None else now
    status = {session.address: session.status for session in sessions}
    retired = []
    with _registry_lock:
        rows = _load()
        for name, row in rows.items():
            if not row.get("address"):
                # A spawn whose registration wait gave up still runs; its pane's process names its session.
                row["address"] = next((s.address for s in sessions
                                       if s.harness == row.get("harness") and owned_by_spawn(s, name)), "")
            if status.get(row.get("address")) != "idle" or "active_at" not in row:
                row["active_at"] = now
            elif now - row["active_at"] >= idle_hours * 3600:
                retired.append(name)
        _save(rows)
    ended = []
    for name in retired:
        try:
            ended.append(stop(name))
        except SpawnError as error:
            logger.warning("could not end idle agent %s: %s", name, error)
    return ended


def stop(target: str) -> dict:
    """End a spawned agent's tmux session by tmux name or address; its harness then releases its registration."""
    with _registry_lock:
        return _stop(target)


def _stop(target: str) -> dict:
    rows = _load()
    name = next((n for n, row in rows.items() if target in (n, row.get("address"))), None)
    if name is None:
        raise NotSpawned(f"no spawned agent matches {target!r}; see `one-conv live spawned`")
    result = subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, text=True, check=False)
    # Only tmux confirming the session gone ends it; a failed kill with that confirmation means it had already ended.
    if _state(name) != "absent":
        # It may still run: keep its record and its listing so it can be reached or stopped again.
        raise SpawnError(f"could not end tmux session {name}: {(result.stderr or '').strip() or result.returncode}")
    row = rows.pop(name)
    _save(rows)
    harness, _, ident = (row.get("address") or "").partition(":")
    if harness == "codex" and ident:
        codex.mark_stopped(ident)
    return row
