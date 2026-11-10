"""Serve this machine's CLIs as remote MCP connectors through the hub.

The bridge publishes each chosen CLI's catalog (`mcp-bridge catalog`) every minute, and
runs every call the hub queues for it with `mcp-bridge call --json`, so the tools keep
using this machine's logins and files while claude.ai reaches them from anywhere.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from . import hubclient
from .model import live_home

logger = logging.getLogger(__name__)

PUBLISH_SECONDS = 60.0
TAKE_WAIT = 20
# mcp-bridge stops a tool after 600 s itself; this bounds a hung bridge process.
CALL_SECONDS = 630
WORKERS = 4


def config_path() -> Path:
    return live_home() / "bridge" / "connectors.json"


def chosen() -> list[str]:
    """The CLIs this machine serves as connectors, by mcp-bridge name (e.g. deezer-cli)."""
    try:
        names = json.loads(config_path().read_text()).get("clis", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [name for name in names if isinstance(name, str)]


def choose(names: list[str]) -> list[str]:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = sorted(set(names))
    path.write_text(json.dumps({"clis": unique}, indent=2) + "\n")
    return unique


def mcp_bridge() -> str:
    """The mcp-bridge launcher; launchd starts the bridge with a short PATH, so ~/.local/bin is tried too."""
    found = shutil.which("mcp-bridge") or str(Path.home() / ".local" / "bin" / "mcp-bridge")
    if not os.access(found, os.X_OK):
        raise FileNotFoundError("mcp-bridge is not installed (expected in ~/.local/bin)")
    return found


def catalog(cli: str) -> dict:
    done = subprocess.run([mcp_bridge(), "catalog", cli], capture_output=True, text=True, timeout=300,
                          stdin=subprocess.DEVNULL)
    if done.returncode:
        raise RuntimeError(f"mcp-bridge catalog {cli} failed: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout)


def run_call(call: dict) -> dict:
    """One relayed tool call; any failure comes back as an MCP error result, never as silence."""
    try:
        done = subprocess.run([mcp_bridge(), "call", call["cli"], call["tool"], json.dumps(call["arguments"]), "--json"],
                              capture_output=True, text=True, timeout=CALL_SECONDS, stdin=subprocess.DEVNULL)
        if done.returncode == 0:
            return json.loads(done.stdout)
        text = f"mcp-bridge exited {done.returncode}: {(done.stderr or done.stdout).strip()[-2000:]}"
    except subprocess.TimeoutExpired:
        text = f"the call did not finish within {CALL_SECONDS}s"
    except (OSError, ValueError) as error:
        text = f"the call could not run: {error}"
    return {"content": [{"type": "text", "text": text}], "isError": True}


class ConnectorRelay:
    """A background thread so a long tool call never delays the bridge's message delivery."""

    def __init__(self, clis=None, *, catalog_of=catalog, run=run_call, client_factory=hubclient.HubClient):
        self.clis = clis
        self.catalog_of, self.run, self.client_factory = catalog_of, run, client_factory
        self.connection: tuple[str, str, str] | None = None
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="connector-call")
        self.catalogs: dict[str, dict] = {}

    def connect(self, url: str, token: str, node: str) -> None:
        """Start, or point at the bridge's current hub connection; does nothing without chosen CLIs."""
        if not (self.clis if self.clis is not None else chosen()):
            return
        self.connection = (url, token, node)
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, name="connector-relay", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def publish(self, client) -> None:
        clis = self.clis if self.clis is not None else chosen()
        for cli in clis:
            try:
                self.catalogs[cli] = self.catalog_of(cli)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                # A CLI that fails introspection keeps its last good catalog rather than vanishing.
                logger.warning("connector %s: %s", cli, error)
        published = {cli: c for cli, c in self.catalogs.items() if cli in clis}
        client.request("PUT", "/v1/connectors", {"catalogs": published})

    def serve_calls(self, client) -> int:
        calls = client.request("GET", f"/v1/connector-calls?wait={TAKE_WAIT}").get("calls", [])
        for call in calls:
            self.pool.submit(self._answer, client, call)
        return len(calls)

    def _answer(self, client, call: dict) -> None:
        result = self.run(call)
        try:
            client.request("POST", f"/v1/connector-calls/{call['id']}", {"result": result})
        except (hubclient.HubUnavailable, hubclient.HubRefused) as error:
            logger.warning("connector call %s: the result could not be posted: %s", call["id"], error)

    def _loop(self) -> None:
        published_at, backoff = 0.0, 1.0
        while not self.stopping.is_set():
            url, token, node = self.connection
            client = self.client_factory(url, token, node, timeout=TAKE_WAIT + 15)
            try:
                if time.monotonic() - published_at >= PUBLISH_SECONDS:
                    self.publish(client)
                    published_at = time.monotonic()
                self.serve_calls(client)
                backoff = 1.0
            except (hubclient.HubUnavailable, hubclient.HubRefused) as error:
                logger.warning("connector relay: %s; retrying in %.0fs", error, backoff)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 30.0)
