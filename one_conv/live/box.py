"""Cloud boxes: relay nodes on a cloud VM that this machine powers on for a teleport and off when idle.

A box is a node joined like any other (`live invite` / `live join --accept-teleport`) whose VM this
machine can start and stop through the provider's CLI (Scaleway's `scw`, already logged in here).
Its disk is kept while it is off, so tools, logins and checkouts installed on it stay for the next
teleport; only compute stops being billed. The registry lives in boxes.json next to the bridge
settings, so only machines that registered a box manage it.

`live teleport` to a box that is off powers it on first. The bridge of a machine that registered
boxes powers a box off once no session on it was busy for IDLE_MINUTES, from the relay's published
sessions; it never touches a box it has not seen running for that long.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from . import bridge

logger = logging.getLogger(__name__)

PROVIDERS = ("scaleway",)
IDLE_MINUTES = 30.0
# How often the bridge looks at its boxes' sessions.
CHECK_SECONDS = 60.0
SCW_TIMEOUT = 300.0
RUNNING = "running"
STOPPED = "stopped"


class BoxError(Exception):
    """A box that cannot be found, started or stopped."""


@dataclass
class Box:
    node: str
    provider: str
    server: str
    zone: str


def registry_path() -> Path:
    return bridge.bridge_dir().parent / "boxes.json"


def load() -> dict[str, Box]:
    try:
        data = json.loads(registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {node: Box(**entry) for node, entry in data.items()}


def save(boxes: dict[str, Box]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({node: asdict(box) for node, box in sorted(boxes.items())}, indent=2) + "\n")


def idle_minutes() -> float:
    """ONE_CONV_BOX_IDLE_MINUTES, else IDLE_MINUTES; 0 never powers a box off."""
    try:
        return float(os.environ.get("ONE_CONV_BOX_IDLE_MINUTES", IDLE_MINUTES))
    except ValueError:
        return IDLE_MINUTES


def _scw(*args: str, run=subprocess.run) -> dict:
    if shutil.which("scw") is None and run is subprocess.run:
        raise BoxError("the Scaleway CLI `scw` is not installed here (brew install scw, then scw init)")
    result = run(["scw", *args, "-o", "json"], check=False, capture_output=True, text=True, timeout=SCW_TIMEOUT)
    if result.returncode:
        raise BoxError(f"scw {' '.join(args[:3])} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def state(box: Box, run=subprocess.run) -> str:
    """The VM's provider state: running, stopped, starting, stopping, ..."""
    return _scw("instance", "server", "get", box.server, f"zone={box.zone}", run=run).get("state", "")


def start(box: Box, run=subprocess.run) -> bool:
    """Power the VM on and wait until it runs; False when it already ran."""
    if state(box, run) == RUNNING:
        return False
    _scw("instance", "server", "start", box.server, f"zone={box.zone}", "-w", run=run)
    return True


def stop(box: Box, run=subprocess.run) -> bool:
    """Power the VM off and keep its disk; False when it was not running."""
    if state(box, run) != RUNNING:
        return False
    _scw("instance", "server", "stop", box.server, f"zone={box.zone}", "-w", run=run)
    return True


def idle_boxes(boxes: dict[str, Box], sessions: list[dict], last_busy: dict[str, float], now: float,
               idle_seconds: float) -> list[str]:
    """Nodes whose sessions were not busy for idle_seconds; updates `last_busy` from `sessions`.

    A node first seen here starts its clock now, so a box is never stopped on the first look.
    """
    for row in sessions:
        if row.get("node") in boxes and row.get("status") == "busy" and row.get("online", True):
            last_busy[row["node"]] = now
    idle = []
    for node in boxes:
        last_busy.setdefault(node, now)
        if idle_seconds > 0 and now - last_busy[node] >= idle_seconds:
            idle.append(node)
    return idle


class Keeper:
    """Powers off this machine's boxes once idle; run from the bridge loop, one stop at a time."""

    def __init__(self, idle_seconds: float | None = None, check_seconds: float = CHECK_SECONDS,
                 clock: Callable[[], float] = time.time, stopper: Callable[[Box], bool] = stop):
        self.idle_seconds = idle_minutes() * 60 if idle_seconds is None else idle_seconds
        self.check_seconds, self.clock, self.stopper = check_seconds, clock, stopper
        self.last_busy: dict[str, float] = {}
        self.checked = None
        self.worker: threading.Thread | None = None

    def poll(self, client) -> list[str]:
        """Check at most every check_seconds; returns the nodes whose stop it started."""
        boxes = load()
        if not boxes or self.idle_seconds <= 0:
            return []
        now = self.clock()
        if self.checked is not None and now - self.checked < self.check_seconds:
            return []
        if self.worker is not None and self.worker.is_alive():
            return []
        self.checked = now
        sessions = client.request("GET", "/v1/sessions").get("sessions", [])
        idle = idle_boxes(boxes, sessions, self.last_busy, now, self.idle_seconds)
        if idle:
            self.worker = threading.Thread(target=self._stop, args=([boxes[node] for node in idle],), daemon=True)
            self.worker.start()
        return idle

    def _stop(self, boxes: list[Box]) -> None:
        for box in boxes:
            try:
                if self.stopper(box):
                    logger.info("powered off box %s: no busy session for %.0f min", box.node, self.idle_seconds / 60)
            except (BoxError, subprocess.TimeoutExpired, OSError) as error:
                logger.warning("could not power off box %s: %s", box.node, error)
            # Restart the clock either way: a failed stop is retried after another idle period.
            self.last_busy[box.node] = self.clock()
