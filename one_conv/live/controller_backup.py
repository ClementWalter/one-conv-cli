"""Keep an off-box copy of the live controller on this machine (`one-conv backup-controller`).

The bridge runs it every ONE_CONV_CONTROLLER_BACKUP_MINUTES (default 60, 0 turns it off). Each run
saves the controller's state as `state/state-<UTC>.tar.gz`, keeping the newest
ONE_CONV_CONTROLLER_BACKUP_KEEP (default 48), then brings `archive.db`, a mirror of the
conversation archive, up to date with only the rows added since the last run. Only a node with
staffing authority may pull; any other node gets 403 and nothing is written.

The copy lives in ONE_CONV_CONTROLLER_BACKUP_DIR, by default `<live_home>/controller-backup`.
`one-conv live control restore --from DIR` rebuilds a controller directory from it.
"""

import json
import logging
import os
import time
from pathlib import Path

from one_conv.control import backup

from . import hubclient
from .model import live_home

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 60.0
DEFAULT_KEEP = 48
# Never fill the disk: the archive pull stops while less than this would stay free.
FREE_FLOOR_BYTES = 2 * 1024**3


def backup_dir() -> Path:
    return Path(os.environ.get("ONE_CONV_CONTROLLER_BACKUP_DIR") or live_home() / "controller-backup")


def backup_minutes() -> float:
    try:
        return float(os.environ.get("ONE_CONV_CONTROLLER_BACKUP_MINUTES", DEFAULT_MINUTES))
    except ValueError:
        return DEFAULT_MINUTES


def keep_count() -> int:
    try:
        return max(1, int(os.environ.get("ONE_CONV_CONTROLLER_BACKUP_KEEP", DEFAULT_KEEP)))
    except ValueError:
        return DEFAULT_KEEP


def save_state(client: hubclient.HubClient, target: Path, keep: int, now: float | None = None) -> Path:
    """Write one state snapshot atomically and drop all but the newest `keep`."""
    _media, body = client.download("/v1/backup/state")
    folder = target / "state"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"state-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now or time.time()))}.tar.gz"
    partial = path.with_suffix(".partial")
    partial.write_bytes(body)
    os.chmod(partial, 0o600)
    partial.replace(path)
    for old in sorted(folder.glob("state-*.tar.gz"))[:-keep]:
        old.unlink()
    return path


def pull_archive(client: hubclient.HubClient, target: Path) -> dict:
    """Bring the archive mirror up to date; heads first, so every head it holds has its rows."""
    mirror = target / "archive.db"
    counts = {}
    for table in backup.ARCHIVE_TABLES:
        after, counts[table] = backup.mirror_cursor(mirror, table), 0
        while True:
            if backup.free_bytes(target) < FREE_FLOOR_BYTES:
                raise OSError(f"less than {FREE_FLOOR_BYTES // 1024**3} GB free at {target}; archive pull stopped")
            page = client.request("GET", f"/v1/backup/archive?table={table}&after={after}")
            known = backup.mirror_generation(mirror)
            if known is not None and known != page["state"]["generation"]:
                # The controller's archive was replaced; the mirror starts over beside the old one.
                mirror.replace(mirror.with_name("archive.previous.db"))
                logger.warning("the controller archive changed generation; started a fresh mirror")
                return pull_archive(client, target)
            counts[table] += backup.apply_page(mirror, page)
            after = page["next"]
            if not page["more"]:
                break
    return counts


def run(client: hubclient.HubClient, target: Path | None = None) -> dict:
    target = target or backup_dir()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = save_state(client, target, keep_count())
    try:
        archive = pull_archive(client, target)
    except hubclient.HubRefused as error:
        if error.status != 404:
            raise
        archive = {}  # The controller keeps no archive.
    result = {"state": state.name, "archive": archive, "at": time.time()}
    (target / "last.json").write_text(json.dumps(result))
    return result


def build_command():
    """`one-conv backup-controller`, run by the bridge."""
    import click

    @click.command("backup-controller", hidden=True, context_settings={"help_option_names": ["-h", "--help"]},
                   epilog="\b\nExamples:\n  one-conv backup-controller\n  ONE_CONV_CONTROLLER_BACKUP_DIR=/Volumes/backup/one-conv one-conv backup-controller")
    def backup_controller():
        """Copy the live controller's state and archive to this machine (ONE_CONV_CONTROLLER_BACKUP_DIR)."""
        try:
            hub = hubclient.configured()
        except hubclient.HubRefused as error:
            raise click.ClickException(str(error)) from None
        if hub is None:
            raise click.ClickException("this machine has no live hub to back up")
        hub.timeout = max(hub.timeout, 300)
        try:
            click.echo(json.dumps(run(hub)))
        except (hubclient.HubRefused, hubclient.HubUnavailable, OSError) as error:
            raise click.ClickException(str(error)) from None

    return backup_controller
