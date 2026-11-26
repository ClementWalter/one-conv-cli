"""`live teleport`: the calling agent session moves itself to another machine on the relay.

The origin takes an inventory of what the session needs (inventory.py), asks the target node's
bridge which of those it has (a relay request answered by provision.py there), and stops at
that table with --check. Otherwise it refuses while anything is missing (unless --force) or
while work exists only here: uncommitted changes always, commits no remote has unless
--push-wip pushes HEAD to `teleport/<id>` (never to the session's own branch). It then syncs
this session to the relay's archive, so the target reads its latest turns, and asks the
target to start the same harness at the same commit, briefed with a handoff seed and told to
confirm to this session first. The session's @name moves with it. This session keeps running
but must stop working: the task continues remotely.

Requests go through the relay under one id each, so a retried post never starts a second
session, and each carries a time to live, so a bridge that comes back late does not act on one.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable

from . import bridge, hubclient, names, provision, spawn
from .inventory import Inventory
from .model import LiveSession

PROBE_TIMEOUT = 60.0
# The target waits SPAWN_TIMEOUT for the harness to register; the origin a little longer for the answer.
SPAWN_WAIT = provision.SPAWN_TIMEOUT + 30.0
# A request not picked up by the target bridge within this is refused there.
REQUEST_TTL = 60.0
ARCHIVE_SYNC_TIMEOUT = 120.0
POST_ATTEMPTS = 3
# Histories `archive-sync.py --source` names for each harness.
ARCHIVE_SOURCE = {"claude": "claude", "codex": "codex", "omp": "omp", "cursor": "cursor-cli", "agy": "agy"}
FINAL = ("accepted", "refused", "unconfirmed")
# Rows that decide where the session works: nothing can start without them, even with --force.
PLACEMENT = ("harness", "repo", "folder")


class TeleportError(Exception):
    """A teleport that cannot go ahead; `screen` is the target's harness screen when it failed to register."""

    def __init__(self, message: str, screen: str = ""):
        super().__init__(message)
        self.screen = screen


def post_request(hub: hubclient.HubClient, node: str, sender: str, request: dict,
                 sleep: Callable[[float], None] = time.sleep) -> str:
    """Post a request to `node`'s bridge under one id, retried on an unreachable relay; returns the id."""
    message = {"id": uuid.uuid4().hex, "to": node, "from": sender, "mode": "request", "reply": "none",
               "text": json.dumps(request)}
    for attempt in range(POST_ATTEMPTS):
        try:
            hub.post(message)
            return message["id"]
        except hubclient.HubRefused as error:
            raise TeleportError(f"the relay refused the {request['kind']} request: {error}") from None
        except hubclient.HubUnavailable as error:
            if attempt == POST_ATTEMPTS - 1:
                raise TeleportError(f"the relay is unreachable: {error}") from None
            sleep(1.0)
    raise AssertionError("unreachable")


def wait_answer(hub: hubclient.HubClient, node: str, message_id: str, timeout: float,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> tuple[str, dict]:
    """(outcome, result) of a request once its bridge answered it with a final outcome."""
    deadline = clock() + timeout
    while True:
        try:
            status = hub.request("GET", f"/v1/messages/{message_id}")
            row = (status.get("recipients") or [{}])[0]
        except hubclient.HubUnavailable:
            row = {}
        if row.get("outcome") in FINAL:
            return row["outcome"], row.get("result") or {}
        if clock() >= deadline:
            state = "is working on it" if row.get("outcome") == "held" else "has not taken it"
            raise TeleportError(f"{node} did not answer within {timeout:g}s ({node}'s bridge {state}); "
                                f"check `one-conv live serve --status` on {node}")
        sleep(1.0)


def ask(hub: hubclient.HubClient, node: str, sender: str, request: dict, timeout: float, **waiting) -> dict:
    """The target bridge's answer to one request; TeleportError when it refused or did not answer."""
    message_id = post_request(hub, node, sender, request, waiting.get("sleep", time.sleep))
    outcome, result = wait_answer(hub, node, message_id, timeout, **waiting)
    if outcome != "accepted":
        error = result.get("error") or (f"{node} {outcome} the request without saying why; its one-conv may "
                                        "predate teleport: update it there (`one-conv update --yes`)")
        raise TeleportError(error, result.get("screen") or "")
    return result


def blockers(inventory: Inventory, rows: list[dict], node: str, force: bool, push_wip: bool) -> list[str]:
    """Why the teleport must not go ahead; empty when it may."""
    reasons = []
    pushing = push_wip and inventory.repo is not None and inventory.repo.unpushed > 0
    for row in rows:
        if row["status"] != "missing":
            continue
        if row["item"] == "commit" and pushing:
            continue  # --push-wip carries the commit over.
        if row["item"] in PLACEMENT or not force:
            reasons.append(f"{row['item']} {row['name']} is missing on {node}" + (f": {row['detail']}" if row["detail"] else ""))
    repo = inventory.repo
    if repo is not None:
        if repo.changed or repo.untracked:
            reasons.append(f"{repo.changed} changed and {repo.untracked} untracked files in {repo.root}: commit them "
                           "(or remove them) first; a teleport moves commits, never a working tree")
        if repo.unpushed and not push_wip:
            reasons.append(f"{repo.unpushed} commits on HEAD are only on this machine: push them, or pass --push-wip "
                           "to push HEAD to a teleport/<id> branch")
        if not repo.remote:
            reasons.append(f"{repo.root} has no origin remote, so {node} cannot fetch its commits")
    return reasons


def push_wip(inventory: Inventory, short: str) -> str:
    """Push the committed HEAD to `teleport/<short>` on origin; returns the branch name."""
    branch = f"teleport/{short}"
    result = subprocess.run(["git", "-C", inventory.repo.root, "push", "origin", f"HEAD:refs/heads/{branch}"],
                            capture_output=True, text=True, timeout=120, check=False,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise TeleportError(f"git push origin HEAD:{branch} failed: {result.stderr.strip()}")
    return branch


def sync_archive(hub: hubclient.HubClient, harness: str, cwd: str, timeout: float = ARCHIVE_SYNC_TIMEOUT) -> str:
    """Upload this session's history to the relay's archive now; returns '' or why it did not finish."""
    from one_conv.archive import HUB_ARCHIVE_PATH
    if not bridge.ARCHIVE_SCRIPT.is_file():
        return "the archive sync script is missing"
    # The token travels in the environment, never in argv where other users could read it.
    env = {**os.environ, "ONE_CONV_ARCHIVE_URL": hub.url + HUB_ARCHIVE_PATH, "ONE_CONV_ARCHIVE_TOKEN": hub.token,
           "ONE_CONV_ARCHIVE_ORIGIN": hub.node or ""}
    command = [sys.executable, str(bridge.ARCHIVE_SCRIPT), "--source", ARCHIVE_SOURCE.get(harness, harness),
               *(["--project", cwd] if cwd else [])]
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return f"the archive sync did not finish within {timeout:g}s"
    except OSError as error:
        return str(error)
    return "" if result.returncode == 0 else (result.stderr.strip().splitlines() or ["archive sync failed"])[-1]


def choose_mode(harness: str, explicit: str | None, detected: str) -> str:
    """--mode when given, else the origin's own mode when this harness has it, else ask."""
    chosen = explicit or detected or "ask"
    try:
        spawn.mode_flags(harness, chosen)
    except ValueError as error:
        if explicit:
            raise TeleportError(str(error)) from None
        return "ask"
    return chosen


def run(node: str, sender: LiveSession, transcript: dict, inventory: Inventory, *, cwd: str | None = None,
        mode: str | None = None, check_only: bool = False, force: bool = False, wip: bool = False,
        hub: hubclient.HubClient | None = None, probe_timeout: float = PROBE_TIMEOUT,
        spawn_wait: float = SPAWN_WAIT, **waiting) -> dict:
    """Check `node`, then (unless check_only or blocked) move `sender` there; returns the report.

    `transcript` carries the session's handoff `seed` and last request `summary`.
    """
    hub = hub or hubclient.configured()
    if hub is None:
        raise TeleportError("this machine is not connected to a relay: run `one-conv live join` with an invite first")
    if node == hub.node:
        raise TeleportError(f"{node} is this machine; teleport to another node")
    origin = f"{hub.node}/{sender.address}"
    report = {"origin": origin, "node": node, "inventory": inventory.to_json()}
    short = uuid.uuid4().hex[:8]
    repo = inventory.repo
    fetch = repo.branch if repo else ""
    probe = ask(hub, node, sender.address, {"kind": "probe", "inventory": inventory.to_json(), "cwd": cwd,
                                            "fetch": fetch, "ttl": REQUEST_TTL}, probe_timeout, **waiting)
    report["rows"], report["dir"] = probe.get("rows") or [], probe.get("dir") or ""
    report["blockers"] = blockers(inventory, report["rows"], node, force, wip)
    if check_only or report["blockers"]:
        report["status"] = "checked" if check_only and not report["blockers"] else "refused"
        provision.audit("origin", kind="probe", node=node, origin=origin, outcome=report["status"],
                        blockers=report["blockers"])
        return report
    chosen = choose_mode(inventory.harness, mode, inventory.mode)
    wip_branch = push_wip(inventory, short) if wip and repo is not None and repo.unpushed else ""
    report["archive"] = sync_archive(hub, inventory.harness, inventory.cwd) or "synced"
    name = names.release(sender.address)
    request = {"kind": "spawn", "inventory": inventory.to_json(), "origin": origin, "cwd": cwd, "mode": chosen,
               "name": name, "short": short, "fetch": wip_branch or fetch, "wip_branch": wip_branch,
               "seed": transcript.get("seed") or "", "summary": transcript.get("summary") or "",
               "timeout": provision.SPAWN_TIMEOUT, "ttl": REQUEST_TTL}
    try:
        started = ask(hub, node, sender.address, request, spawn_wait, **waiting)
    except TeleportError:
        if name:
            names.set_name(name, sender.address, lambda address: False)
        provision.audit("origin", kind="spawn", node=node, origin=origin, outcome="failed")
        raise
    report.update({key: started.get(key) for key in ("address", "tmux", "dir", "branch", "sha", "checkout", "name")
                   if started.get(key)})
    if started.get("name_error"):
        report["name_error"] = started["name_error"]
    report.update(status="teleported", mode=chosen, wip_branch=wip_branch,
                  next=(f"Stop working now: this task continues on {node} as {started.get('address')}, which "
                        f"confirms here with one message. Do not edit files or send messages for this task from "
                        f"this session."))
    provision.audit("origin", kind="spawn", node=node, origin=origin, outcome="teleported",
                    address=started.get("address"), dir=started.get("dir"))
    return report


def table(rows: list[dict]) -> list[str]:
    """One line per checked item: status, kind, name, detail."""
    return [f"  {row['status']:<8} {row['item']:<8} {row['name']}" + (f"  ({row['detail']})" if row["detail"] else "")
            for row in rows]
