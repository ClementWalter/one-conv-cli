"""Task actions for the agent on this machine: through the hub when one is configured, else locally.

With a hub, the hub holds every task and checks that the actor is a session this machine
published. Without one, this machine keeps its own task store (`tasks.sqlite` under the live
home) with the same rules; the person at this machine is then the human. A configured hub that
cannot be reached is an error: a claim is never made locally behind its back.
"""

import sqlite3
from contextlib import closing

from . import hubclient, tasks
from .model import live_home


class TaskOpError(Exception):
    """A task action that did not happen; `task` is the current state when the store knows it."""

    def __init__(self, message: str, task: dict | None = None):
        super().__init__(message)
        self.task = task


def _local() -> sqlite3.Connection:
    home = live_home()
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "tasks.sqlite", timeout=30)
    with db:
        tasks.ensure(db)
    return db


def local_call(work):
    """Run `work(db)` in one immediate transaction on this machine's task store."""
    with closing(_local()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        try:
            return work(db)
        except tasks.TaskError as error:
            raise TaskOpError(str(error), error.task) from None


def _hub():
    try:
        return hubclient.configured()
    except hubclient.HubRefused as error:
        raise TaskOpError(str(error)) from error


def _remote(work):
    try:
        return work()
    except hubclient.HubRefused as error:
        raise TaskOpError(str(error)) from error
    except hubclient.HubUnavailable as error:
        # The hub may have applied the action before the answer was lost: the outcome is unconfirmed.
        raise TaskOpError(f"{error}. The outcome is unconfirmed: the hub may have applied it. Check it with "
                          "`one-conv live task show KEY`, or run the same command again; the hub answers a repeat "
                          "with the task as it is. Nothing was done locally instead.") from error


def show(key: str) -> dict:
    hub = _hub()
    if hub is not None:
        return _remote(lambda: hub.task(key))
    return local_call(lambda db: {**tasks.require(db, tasks.check_key(key)),
                                  "children": tasks.children(db, key), "events": tasks.events(db, key)})


def listing(topic: str | None = None, owner: str | None = None, open_only: bool = False) -> list[dict]:
    hub = _hub()
    if hub is not None:
        return _remote(lambda: hub.tasks(topic, owner, open_only))
    return local_call(lambda db: tasks.listing(db, topic=topic, owner=owner, open_only=open_only))


def claim(key: str, address: str, version: int | None = None) -> dict:
    hub = _hub()
    if hub is not None:
        return _remote(lambda: hub.task_action("claim", {"key": key, "actor": address, "version": version}))
    before = local_call(lambda db: tasks.require(db, tasks.check_key(key)))
    task = local_call(lambda db: tasks.claim(db, key, address, version))
    if task["version"] != before["version"]:
        from . import router
        # The claimer had the request as an offer; it now gets the owner's instructions.
        router.deliver_task(address, f"You claimed task {task['key']} (version {task['version']}).\n{task['title']}",
                            "user", task, "owner")
    return task


def update(key: str, address: str, version: int, state: str | None = None, note: str | None = None) -> dict:
    hub = _hub()
    body = {"key": key, "actor": address, "version": version, "state": state, "note": note}
    if hub is not None:
        return _remote(lambda: hub.task_action("update", body))
    return local_call(lambda db: tasks.update(db, key, address, version, state, note))


def delegate(key: str, address: str, version: int, to: str, kind: str, note: str | None = None,
             round_: int = 1) -> dict:
    hub = _hub()
    body = {"key": key, "actor": address, "version": version, "to": to, "kind": kind, "note": note,
            "round": round_}
    if hub is not None:
        return _remote(lambda: hub.task_action("delegate", body))
    from . import router, team
    route = team.address_for(to[1:]) if to.startswith("@") else to
    if not route:
        raise TaskOpError(f"no agent here is named {to}; see `one-conv live names`")
    child, created = local_call(lambda db: tasks.delegate(db, key, address, version, route, kind, note, round_))
    if created:
        # The collaborator is woken with only its part; it reports to the owner.
        router.deliver_task(route, f"Child task {child['key']} ({kind}) from {address}:\n{child['note'] or '(no note)'}"
                            f"\nRequest: {child['title']}\nWork only this part and report to the task owner.",
                            address, child, "collaborator")
    return child
