"""`live teleport`: the calling agent session moves itself to another machine on the relay.

The origin takes an inventory of what the session needs (inventory.py) and asks the target node's
bridge which of those it has (a relay request answered by provision.py there). What the target
lacks never refuses the move: it becomes a provisioning brief (brief.py), saved here and shipped
with the move, that the continuation works through as step 0. Only a missing tmux, a harness the
target cannot install without a person, or a --cwd that is another repository (without --force)
stops it; --check stops after printing the table and the brief.

Work that exists only here travels as a snapshot (snapshot.py): uncommitted files are committed
on top of HEAD without touching the checkout and pushed with unpushed commits to `teleport/<id>`,
never to the session's own branch. The session is then synced to the relay's archive, so the
target reads its latest turns, and the target starts the same harness at that commit, briefed
with a handoff seed and told to confirm to this session first. The session's @name moves with it.
This session keeps running but must stop working: the task continues remotely.

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

from . import bridge, brief, hubclient, names, provision, snapshot, spawn
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


class TeleportError(Exception):
    """A teleport that cannot go ahead; `screen` is the target's harness screen when it failed to register."""

    def __init__(self, message: str, screen: str = ""):
        super().__init__(message)
        self.screen = screen


class NoAnswer(TeleportError):
    """The target's bridge did not answer in time: it may be down, or still booting."""


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
            raise NoAnswer(f"{node} did not answer within {timeout:g}s ({node}'s bridge {state}); "
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


def blockers(rows: list[dict], force: bool) -> list[str]:
    """Why the teleport cannot start at all; empty when it may (whatever else the brief lists)."""
    reasons = []
    for row in rows:
        if row["item"] == "tmux" and row["status"] == "missing":
            reasons.append("tmux is missing on the target, and the continuation runs in tmux")
        elif row["item"] == "harness" and row["status"] == "missing" and not row.get("install"):
            reasons.append(f"{row['name']} is missing on the target and cannot be installed there without a person")
        elif row["status"] == "dubious" and not force:
            reasons.append(row["detail"])
    return reasons


def needs_snapshot(inventory: Inventory, rows: list[dict]) -> bool:
    """Whether work exists only here: changed or untracked files, unpushed commits, or a commit the target cannot fetch."""
    repo = inventory.repo
    if repo is None or not repo.remote or not repo.sha:
        return False
    unfetchable = any(row["item"] == "commit" and row["status"] == "missing" for row in rows)
    return bool(repo.changed or repo.untracked or repo.unpushed or unfetchable)


def plan_snapshot(inventory: Inventory) -> dict:
    """What a snapshot would carry and leave out, without committing or pushing anything."""
    carried, skipped = snapshot.plan(inventory.repo.root)
    return {"files": len(carried), "unpushed": inventory.repo.unpushed, "skipped": skipped}


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
        mode: str | None = None, check_only: bool = False, force: bool = False,
        hub: hubclient.HubClient | None = None, probe_timeout: float = PROBE_TIMEOUT,
        spawn_wait: float = SPAWN_WAIT, probe_attempts: int = 1, **waiting) -> dict:
    """Check `node`, write the provisioning brief, then (unless check_only or blocked) move `sender` there.

    `transcript` carries the session's handoff `seed` and last request `summary`. `probe_attempts`
    re-asks a target that did not answer, for a box still booting.
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
    for attempt in range(probe_attempts):
        try:
            probe = ask(hub, node, sender.address, {"kind": "probe", "inventory": inventory.to_json(), "cwd": cwd,
                                                    "fetch": fetch, "ttl": REQUEST_TTL}, probe_timeout, **waiting)
            break
        except NoAnswer:
            if attempt == probe_attempts - 1:
                raise
    report["rows"], report["dir"] = probe.get("rows") or [], probe.get("dir") or ""
    report["blockers"] = blockers(report["rows"], force)
    pending = plan_snapshot(inventory) if needs_snapshot(inventory, report["rows"]) else None
    if check_only or report["blockers"]:
        text = brief.render(inventory.to_json(), report["rows"], node, probe.get("platform") or "", origin, pending)
        if text:
            report["brief"], report["brief_path"] = text, str(brief.save(text, short))
        if pending:
            report["snapshot"] = {**pending, "planned": True}
        report["status"] = "checked" if check_only and not report["blockers"] else "refused"
        provision.audit("origin", kind="probe", node=node, origin=origin, outcome=report["status"],
                        blockers=report["blockers"], brief=report.get("brief_path"))
        return report
    chosen = choose_mode(inventory.harness, mode, inventory.mode)
    taken = None
    if pending:
        try:
            taken = snapshot.take(repo.root, short).to_json()
        except snapshot.SnapshotError as error:
            raise TeleportError(f"the work only this machine has could not travel: {error}") from None
        report["snapshot"] = taken
    text = brief.render(inventory.to_json(), report["rows"], node, probe.get("platform") or "", origin, taken)
    if text:
        report["brief"], report["brief_path"] = text, str(brief.save(text, short))
    report["archive"] = sync_archive(hub, inventory.harness, inventory.cwd) or "synced"
    name = names.release(sender.address)
    wip_branch = (taken or {}).get("branch") or ""
    spawned_inventory = inventory.to_json()
    if taken and taken.get("commit") and spawned_inventory.get("repo"):
        spawned_inventory["repo"]["sha"] = taken["commit"]
    request = {"kind": "spawn", "inventory": spawned_inventory, "origin": origin, "cwd": cwd, "mode": chosen,
               "name": name, "short": short, "fetch": wip_branch or fetch, "wip_branch": wip_branch,
               "snapshot": taken, "provisioning": text, "force": force,
               "seed": transcript.get("seed") or "", "summary": transcript.get("summary") or "",
               "timeout": provision.SPAWN_TIMEOUT, "ttl": REQUEST_TTL}
    try:
        started = ask(hub, node, sender.address, request, spawn_wait, **waiting)
    except TeleportError:
        if name:
            names.set_name(name, sender.address, lambda address: False)
        provision.audit("origin", kind="spawn", node=node, origin=origin, outcome="failed", snapshot=taken)
        raise
    report.update({key: started.get(key) for key in ("address", "tmux", "dir", "branch", "sha", "checkout", "name",
                                                       "installed") if started.get(key)})
    if started.get("name_error"):
        report["name_error"] = started["name_error"]
    report.update(status="teleported", mode=chosen, wip_branch=wip_branch,
                  next=(f"Stop working now: this task continues on {node} as {started.get('address')}, which "
                        f"confirms here with one message"
                        + (" and provisions what the brief lists" if text else "")
                        + ". Do not edit files or send messages for this task from this session."))
    provision.audit("origin", kind="spawn", node=node, origin=origin, outcome="teleported",
                    address=started.get("address"), dir=started.get("dir"), snapshot=taken,
                    brief=report.get("brief_path"))
    return report


def table(rows: list[dict]) -> list[str]:
    """One line per checked item: status, kind, name, detail."""
    return [f"  {row['status']:<8} {row['item']:<8} {row['name']}" + (f"  ({row['detail']})" if row["detail"] else "")
            for row in rows]
