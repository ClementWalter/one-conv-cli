"""A teleport's snapshot of uncommitted work: a commit on top of HEAD, made without touching the checkout.

The snapshot is built in a temporary index (`GIT_INDEX_FILE`) from HEAD's tree plus every changed,
deleted and untracked path git does not ignore, then written with `git commit-tree -p HEAD`, so the
working tree, the real index and the current branch stay byte-identical and no hook runs. It is
pushed (with `--no-verify`) to `refs/heads/teleport/<id>` on origin, never to the session's branch;
commits not yet pushed travel with it, since its parent is HEAD. A clean tree with unpushed commits
pushes HEAD itself there, and a clean, pushed tree needs nothing.

Files that look like secrets (`.env*`, keys, certificates, credential JSON) or exceed SIZE_LIMIT
are left out even when git does not ignore them, and listed for the person to copy by hand.
"""

import fnmatch
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
                   "credentials*.json", "*.p12", "*.pfx", "*.keystore", "*.jks")
SIZE_LIMIT = 10_000_000
MESSAGE = "wip(teleport): {short}"
# commit-tree needs an author; a machine without git identity still snapshots under this one.
FALLBACK_IDENTITY = {"GIT_AUTHOR_NAME": "one-conv teleport", "GIT_AUTHOR_EMAIL": "teleport@one-conv.invalid",
                     "GIT_COMMITTER_NAME": "one-conv teleport", "GIT_COMMITTER_EMAIL": "teleport@one-conv.invalid"}


class SnapshotError(Exception):
    """The work could not be snapshotted or pushed; the checkout is unchanged."""


@dataclass
class Snapshot:
    """What `take` pushed: the commit and branch, and which paths it carries or left out."""

    branch: str = ""
    commit: str = ""
    # Paths changed, added or deleted relative to HEAD that the snapshot carries.
    files: int = 0
    unpushed: int = 0
    skipped: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def _git(root: str, *args: str, env: dict | None = None, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", root, *args], capture_output=True, input=data, timeout=300, check=False,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", **(env or {})})
    if result.returncode:
        raise SnapshotError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def secret_reason(path: str, size: int | None) -> str:
    """Why `path` stays out of the snapshot, or '' when it travels."""
    name = os.path.basename(path)
    if any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS):
        return "looks like a secret"
    if size is not None and size > SIZE_LIMIT:
        return f"larger than {SIZE_LIMIT // 1_000_000} MB"
    return ""


def changed_paths(root: str) -> list[str]:
    """Every path git status reports, untracked files listed one by one; ignored files are not."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    return [entry[3:].decode(errors="surrogateescape") for entry in out.split(b"\0") if len(entry) > 3]


def plan(root: str) -> tuple[list[str], list[dict]]:
    """(paths a snapshot carries, [{path, reason}] it leaves out); reads the checkout, changes nothing."""
    carried, skipped = [], []
    for path in changed_paths(root):
        full = Path(root) / path
        size = full.stat().st_size if full.is_file() else None
        reason = secret_reason(path, size)
        if reason:
            skipped.append({"path": path, "reason": reason})
        else:
            carried.append(path)
    return carried, skipped


def take(root: str, short: str, push=True) -> Snapshot:
    """Push the work only this checkout has to `teleport/<short>`; returns what was carried and left out.

    HEAD itself is pushed when nothing is uncommitted, so a commit the target cannot fetch still travels.
    """
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    unpushed = int(_git(root, "rev-list", "--count", "HEAD", "--not", "--remotes").decode().strip() or 0)
    carried, skipped = plan(root)
    found = Snapshot(unpushed=unpushed, skipped=skipped, files=len(carried))
    commit = head
    if carried:
        with tempfile.TemporaryDirectory(prefix="one-conv-teleport-") as scratch:
            index = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
            _git(root, "read-tree", "HEAD", env=index)
            # Literal pathspecs from stdin: any file name works, and deleted paths are recorded as removed.
            _git(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
                 env={**index, "GIT_LITERAL_PATHSPECS": "1"}, data=b"\0".join(p.encode(errors="surrogateescape")
                                                                            for p in carried))
            tree = _git(root, "write-tree", env=index).decode().strip()
        message = MESSAGE.format(short=short).encode()
        try:
            commit = _git(root, "commit-tree", tree, "-p", head, data=message).decode().strip()
        except SnapshotError:
            commit = _git(root, "commit-tree", tree, "-p", head, env=FALLBACK_IDENTITY, data=message).decode().strip()
    found.commit, found.branch = commit, f"teleport/{short}"
    if push:
        _git(root, "push", "--no-verify", "origin", f"{commit}:refs/heads/{found.branch}")
    return found
