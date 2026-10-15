"""Start a coding agent detached in tmux, so it stays resident and reachable by `live send`.

A headless run (`codex exec`, `claude -p`) exits after its turn, so a message queued for it
is never read. An interactive harness in a detached tmux session keeps running between
turns, registers like any open session, and can be watched with `tmux attach` when wanted.
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import codex, omp, router
from .model import LiveSession, live_home

HARNESSES = ("claude", "codex", "omp")
# Each harness's interactive command with a first prompt; the prompt is one argv element.
COMMANDS = {
    "claude": lambda prompt, model: ["claude", *(["--model", model] if model else []), prompt],
    "codex": lambda prompt, model: ["codex", *(["--model", model] if model else []), prompt],
    "omp": lambda prompt, model: ["omp", *([f"--model={model}"] if model else []), prompt],
}
SESSION_PREFIX = "oneconv-"


class SpawnError(Exception):
    """A spawn that cannot start, or a started agent that never registered."""


@dataclass
class Spawned:
    tmux: str
    harness: str
    cwd: str
    address: str = ""

    def to_json(self):
        return {"tmux": self.tmux, "harness": self.harness, "cwd": self.cwd, "address": self.address,
                "attach": f"tmux attach -t {self.tmux}"}


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


def command(harness: str, prompt: str, model: str | None, extra: tuple[str, ...]) -> list[str]:
    """The argv tmux runs directly, without a shell, so the prompt stays literal."""
    if harness not in COMMANDS:
        raise SpawnError(f"cannot spawn {harness!r}; choose one of {', '.join(HARNESSES)}")
    if not prompt.strip():
        raise SpawnError("a spawned agent needs a first prompt")
    argv = COMMANDS[harness](prompt, model)
    return [argv[0], *extra, *argv[1:]]


def preflight(harness: str) -> None:
    """Refuse a spawn that could not end up reachable, before anything starts."""
    if shutil.which("tmux") is None:
        raise SpawnError("live spawn needs tmux on PATH")
    if shutil.which(harness) is None:
        raise SpawnError(f"{harness} is not on PATH")
    if harness == "omp" and not omp.extension_path().exists():
        # Without the extension an omp session never registers, so nothing could message it.
        raise SpawnError("omp sessions register only with the live extension; run `one-conv live install --yes` first")


def start(harness: str, prompt: str, cwd: Path, model: str | None = None, extra: tuple[str, ...] = (),
          wait_seconds: float = 60.0, trust_folder: bool = False) -> Spawned:
    """Start the agent detached in tmux, then wait until it is listed as a live session.

    `trust_folder` answers Claude Code's first-visit folder-trust question with yes; without
    it, a spawn into a folder Claude has not been trusted in stops and says so.
    """
    preflight(harness)
    name = f"{SESSION_PREFIX}{harness}-{uuid.uuid4().hex[:8]}"
    if harness == "codex":
        # Codex threads carry no pid, so the spawn name in the first prompt is what identifies this one.
        prompt = f"{prompt}\n\n(one-conv spawn {name})"
    argv = command(harness, prompt, model, extra)
    before = {s.address for s in router.list_all(harness)}
    result = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", str(cwd), "-x", "200", "-y", "50",
                             "--", *argv], capture_output=True, text=True, check=False)
    if result.returncode:
        raise SpawnError(f"tmux could not start {harness}: {result.stderr.strip() or result.returncode}")
    spawned = Spawned(name, harness, str(cwd))
    rows = _load()
    rows[name] = spawned.to_json()
    _save(rows)
    spawned.address = wait_for_session(harness, cwd, before, name, wait_seconds, trust_folder)
    rows = _load()
    rows[name] = spawned.to_json()
    _save(rows)
    return spawned


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


def _pane(tmux_name: str) -> str:
    return subprocess.run(["tmux", "capture-pane", "-p", "-t", tmux_name], capture_output=True, text=True,
                          check=False).stdout


def wait_for_session(harness: str, cwd: Path, before: set[str], tmux_name: str, wait_seconds: float,
                     trust_folder: bool = False) -> str:
    """The address of the session started in tmux session `tmux_name`; refuses if it never registers."""
    deadline = time.monotonic() + wait_seconds
    resolved = str(cwd.resolve())
    answered = False
    while time.monotonic() < deadline:
        if not _alive(tmux_name):
            raise SpawnError(f"{harness} exited before registering; inspect it with a manual run in {cwd}")
        if harness == "claude" and not answered and TRUST_QUESTION in _pane(tmux_name):
            if not trust_folder:
                raise SpawnError(f"Claude Code is asking whether to trust {cwd}. Answer it with "
                                 f"`tmux attach -t {tmux_name}`, or spawn again with --trust-folder.")
            # Move from the default "No, exit" to "Yes, I trust this folder", then confirm.
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Down"], check=False)
            time.sleep(0.5)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=False)
            answered = True
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
    rows = _load()
    alive = {name: row for name, row in rows.items() if _alive(name)}
    if alive.keys() != rows.keys():
        _save(alive)
    return list(alive.values())


def stop(target: str) -> dict:
    """End a spawned agent's tmux session by tmux name or address; its harness then releases its registration."""
    rows = _load()
    name = next((n for n, row in rows.items() if target in (n, row.get("address"))), None)
    if name is None:
        raise SpawnError(f"no spawned agent matches {target!r}; see `one-conv live spawned`")
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
