"""Read bounded runtime metadata from an existing Codex daemon without starting or resuming work."""

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

TIMEOUT_SECONDS = 2.0
MAX_THREADS = 128
MAX_FRAMES = 128
MAX_PAGES = 4
CLIENT_INFO = {"clientInfo": {"name": "one_conv_presence", "version": "1.0"}}
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class ReadUnavailable(Exception):
    """Native runtime evidence is unavailable; lock-based presence remains unverified."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Codex presence deadline expired")
    return remaining


def _daemon_pid(home):
    """A private-profile PID record cannot follow a symlink or block on a FIFO."""
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    try:
        descriptor = os.open(Path(home) / "app-server-daemon/daemon.pid", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_size > 32_000:
                return None
            value = json.loads(handle.read(32_001))
        pid = value.get("pid") if isinstance(value, dict) else None
        return pid if type(pid) is int and 0 < pid < 2**31 else None
    except (OSError, ValueError, RecursionError):
        return None


def _run(argv, deadline):
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=_remaining(deadline))
    if result.returncode or len(result.stdout) > 256_000:
        raise ReadUnavailable("Codex daemon discovery unavailable")
    return result.stdout


def _sockets(home, deadline):
    pid = _daemon_pid(home)
    if pid is None:
        return []
    process = _run(["ps", "-o", "uid=,comm=", "-p", str(pid)], deadline).strip().split(None, 1)
    if len(process) != 2 or process[0] != str(os.getuid()) or Path(process[1]).name not in ("codex", "codex.exe"):
        return []
    listing = _run(["lsof", "-n", "-a", "-p", str(pid), "-U", "-Fn"], deadline)
    sockets = []
    for line in listing.splitlines():
        _remaining(deadline)
        if not line.startswith("n/") or line[1:] in sockets:
            continue
        path = Path(line[1:])
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid():
            sockets.append(str(path))
        if len(sockets) == 4:
            break
    return sockets


class Reader:
    """The only requests permitted on this connection inspect loaded-thread metadata."""

    def __init__(self, connection, deadline):
        self.connection, self.deadline, self.ident = connection, deadline, 0

    def _send(self, value):
        # Socket writes can block even when response reads have their own deadline.
        self.connection.socket.settimeout(_remaining(self.deadline))
        self.connection.send(json.dumps(value))

    def call(self, method, params):
        allowed = (method == "initialize" and params == CLIENT_INFO or
                   method == "thread/loaded/list" and isinstance(params, dict) and set(params) <= {"cursor"}
                   and (not params or isinstance(params["cursor"], str) and len(params["cursor"]) <= 512) or
                   method == "thread/read" and isinstance(params, dict) and set(params) == {"threadId", "includeTurns"}
                   and isinstance(params["threadId"], str) and THREAD_ID.fullmatch(params["threadId"])
                   and params["includeTurns"] is False)
        if not allowed:
            raise ValueError("Only read-only Codex presence requests are permitted")
        _remaining(self.deadline)
        self.ident += 1
        self._send({"id": self.ident, "method": method, "params": params})
        for _ in range(MAX_FRAMES):
            value = json.loads(self.connection.recv(timeout=_remaining(self.deadline)))
            if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != self.ident:
                continue
            if "error" in value or not isinstance(value.get("result"), dict):
                raise ReadUnavailable("Codex presence request unavailable")
            return value["result"]
        raise ReadUnavailable("Codex presence notification limit reached")

    def initialized(self):
        _remaining(self.deadline)
        self._send({"method": "initialized", "params": {}})


def _connect(path, deadline):
    from websockets.sync.client import ClientConnection, unix_connect

    class DeadlineConnection(ClientConnection):
        """Protocol replies share the absolute deadline of application RPC writes."""

        def send_data(self):
            self.socket.settimeout(_remaining(deadline))
            super().send_data()

    return unix_connect(path, uri="ws://localhost", open_timeout=min(0.5, _remaining(deadline)),
                        close_timeout=0, max_size=1_048_576, max_queue=16, ping_interval=None,
                        proxy=None, create_connection=DeadlineConnection)


def _text(value, maximum=4096):
    return value if isinstance(value, str) and len(value) <= maximum and "\x00" not in value else ""


def _summary(result, ident):
    thread = result.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != ident:
        raise ReadUnavailable("Codex presence returned a different thread")
    status = thread.get("status")
    kind = status.get("type") if isinstance(status, dict) else None
    source = thread.get("source")
    agent = source.get("subAgent", {}) if isinstance(source, dict) else {}
    spawned = agent.get("thread_spawn", {}) if isinstance(agent, dict) else {}
    path = _text(spawned.get("agent_path"), 256) if isinstance(spawned, dict) else ""
    # An idle internal child can be completed but retained; only its parent can establish autonomous availability.
    state = "loaded" if kind == "idle" and isinstance(source, dict) and "subAgent" in source else {"active": "busy", "idle": "idle"}.get(kind, "loaded")
    return {"status": state,
            "title": _text(thread.get("name"), 1024) or path, "cwd": _text(thread.get("cwd"))}


def _read(connection, deadline):
    reader = Reader(connection, deadline)
    reader.call("initialize", CLIENT_INFO)
    reader.initialized()
    identifiers, known, cursor, cursors = [], set(), None, set()
    for _ in range(MAX_PAGES):
        result = reader.call("thread/loaded/list", {} if cursor is None else {"cursor": cursor})
        data = result.get("data")
        if not isinstance(data, list):
            raise ReadUnavailable("Codex loaded-thread list unavailable")
        for ident in data:
            _remaining(deadline)
            if isinstance(ident, str) and THREAD_ID.fullmatch(ident) and ident not in known:
                identifiers.append(ident)
                known.add(ident)
            if len(identifiers) == MAX_THREADS:
                break
        cursor = result.get("nextCursor")
        if cursor is None or len(identifiers) == MAX_THREADS:
            break
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ReadUnavailable("Codex loaded-thread cursor unavailable")
        cursors.add(cursor)
    threads = {}
    for ident in identifiers:
        try:
            threads[ident] = _summary(reader.call("thread/read", {"threadId": ident, "includeTurns": False}), ident)
        except ReadUnavailable:
            continue
    return threads


def threads(home):
    """Return runtime state and native title/cwd evidence within one shared discovery/RPC deadline."""
    try:
        from websockets.exceptions import WebSocketException
    except ImportError:
        return {}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        paths = _sockets(home, deadline)
    except (OSError, ValueError, subprocess.SubprocessError, ReadUnavailable, TimeoutError):
        return {}
    for path in paths:
        try:
            with _connect(path, deadline) as connection:
                return _read(connection, deadline)
        except (ImportError, OSError, ValueError, TypeError, RecursionError, WebSocketException, ReadUnavailable, TimeoutError):
            continue
    return {}
