"""Names agents choose for themselves: `@name` stands for a session address.

A name is an alias; messages still route and reply by address, so renaming never breaks a
reply. Names live on this machine, and the bridge publishes them to the relay so `@name`
reaches a session on another machine too.
"""

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from .model import live_home

try:
    import fcntl
except ImportError:  # Windows has no flock; the names file is then replaced unlocked.
    fcntl = None

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class NameRefused(Exception):
    """A name request that cannot be honoured as asked."""


def _check(value: str) -> str:
    value = value.lstrip("@").lower()
    if not NAME.fullmatch(value):
        raise NameRefused(f"a name is 1-32 of a-z, 0-9 and '-', starting with a letter or digit; got {value!r}")
    return value


def _root() -> Path:
    return live_home() / "team"


@contextmanager
def _locked():
    _root().mkdir(parents=True, exist_ok=True)
    with (_root() / ".lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read(path: Path, default):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write(path: Path, data) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    partial.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(partial, path)


def names() -> dict[str, str]:
    """name -> address."""
    return _read(_root() / "names.json", {})


def set_name(name: str, address: str, is_live, replace: bool = False) -> str:
    """Bind `name` to `address`. A name held by another session is refused, never taken silently.

    `replace` takes a name whose holder is not running; a running holder keeps it.
    """
    name = _check(name)
    with _locked():
        table = names()
        holder = table.get(name)
        if holder and holder != address:
            if is_live(holder):
                raise NameRefused(f"@{name} belongs to {holder}, which is running; choose another name")
            if not replace:
                raise NameRefused(f"@{name} belongs to {holder}, which is not running; "
                                "pass --replace to take it")
        # One name per session: choosing a new one releases the old.
        table = {n: a for n, a in table.items() if a != address}
        table[name] = address
        _write(_root() / "names.json", table)
    return name


def release(address: str) -> str | None:
    """Unbind the name `address` holds, so another session (here or on another machine) can take it; returns it."""
    with _locked():
        table = names()
        held = next((n for n, a in table.items() if a == address), None)
        if held is not None:
            _write(_root() / "names.json", {n: a for n, a in table.items() if a != address})
    return held


def address_for(name: str) -> str | None:
    return names().get(_check(name))


def name_for(address: str) -> str | None:
    return next((n for n, a in names().items() if a == address), None)
