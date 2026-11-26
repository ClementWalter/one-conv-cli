"""The teleport snapshot: uncommitted work pushed to teleport/<id> while the checkout stays byte-identical."""

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from one_conv.live import snapshot

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def state(clone: Path) -> tuple:
    """Everything the snapshot must leave alone: status, HEAD, branch, the index's bytes."""
    return (git(clone, "status", "--porcelain", "--untracked-files=all"), git(clone, "rev-parse", "HEAD"),
            git(clone, "symbolic-ref", "HEAD"), hashlib.sha256((clone / ".git" / "index").read_bytes()).hexdigest())


@pytest.fixture
def clone(tmp_path):
    """A pushed clone with tracked files, a .gitignore, and hooks that leave a mark if they ever run."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", str(remote), str(work))
    (work / "keep.txt").write_text("keep")
    (work / "gone.txt").write_text("gone")
    (work / ".gitignore").write_text("build/\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "base")
    git(work, "push", "origin", "main")
    for hook in ("pre-commit", "commit-msg", "pre-push", "post-commit"):
        path = work / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\ntouch {tmp_path}/hook-{hook}-ran\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return SimpleNamespace(work=work, remote=remote, root=tmp_path)


@pytest.fixture
def dirty(clone):
    """Staged, unstaged, deleted, untracked, ignored and secret-looking changes; then a snapshot taken."""
    (clone.work / "keep.txt").write_text("edited")
    (clone.work / "staged.txt").write_text("staged")
    git(clone.work, "add", "staged.txt")
    (clone.work / "gone.txt").unlink()
    (clone.work / "notes").mkdir()
    (clone.work / "notes" / "new.md").write_text("new")
    (clone.work / "build").mkdir()
    (clone.work / "build" / "out.bin").write_text("ignored")
    (clone.work / ".env").write_text("SECRET=1")
    (clone.work / "server.pem").write_text("-----BEGIN")
    before = state(clone.work)
    taken = snapshot.take(str(clone.work), "s1")
    return SimpleNamespace(**vars(clone), before=before, taken=taken,
                           tree=set(git(clone.remote, "ls-tree", "-r", "--name-only", "teleport/s1").splitlines()))


def test_the_working_tree_index_and_branch_are_unchanged(dirty):
    assert state(dirty.work) == dirty.before


def test_no_hook_runs(dirty):
    assert list(dirty.root.glob("hook-*-ran")) == []


def test_the_snapshot_is_pushed_to_the_teleport_branch(dirty):
    assert git(dirty.remote, "rev-parse", "teleport/s1") == dirty.taken.commit


def test_the_sessions_branch_on_the_remote_is_untouched(dirty):
    assert git(dirty.remote, "rev-parse", "main") == git(dirty.work, "rev-parse", "HEAD")


def test_the_snapshot_sits_on_head(dirty):
    assert git(dirty.remote, "rev-parse", "teleport/s1^") == git(dirty.work, "rev-parse", "HEAD")


def test_the_snapshot_is_named_for_the_teleport(dirty):
    assert git(dirty.remote, "log", "-1", "--format=%s", "teleport/s1") == "wip(teleport): s1"


@pytest.mark.parametrize(("path", "carried"), [
    ("keep.txt", True),
    ("staged.txt", True),
    ("notes/new.md", True),
    ("gone.txt", False),
    ("build/out.bin", False),
    (".env", False),
    ("server.pem", False),
])
def test_the_snapshot_tree_holds_the_work_but_not_ignored_or_secret_files(dirty, path, carried):
    assert (path in dirty.tree) == carried


def test_the_edited_content_travels(dirty):
    assert git(dirty.remote, "show", "teleport/s1:keep.txt") == "edited"


def test_skipped_files_are_listed(dirty):
    assert {entry["path"] for entry in dirty.taken.skipped} == {".env", "server.pem"}


def test_the_count_covers_carried_paths(dirty):
    assert dirty.taken.files == 4


def test_unpushed_commits_ride_along(clone):
    (clone.work / "c.txt").write_text("c")
    git(clone.work, "add", ".")
    git(clone.work, "commit", "-m", "local only")
    (clone.work / "d.txt").write_text("d")
    snapshot.take(str(clone.work), "s2")
    assert git(clone.remote, "log", "--format=%s", "teleport/s2~1", "-1") == "local only"


def test_a_clean_tree_with_unpushed_commits_pushes_head(clone):
    (clone.work / "c.txt").write_text("c")
    git(clone.work, "add", ".")
    git(clone.work, "commit", "-m", "local only")
    snapshot.take(str(clone.work), "s3")
    assert git(clone.remote, "rev-parse", "teleport/s3") == git(clone.work, "rev-parse", "HEAD")


@pytest.mark.parametrize(("path", "size", "skipped"), [
    (".env", 10, True),
    (".env.local", 10, True),
    ("config/prod.key", 10, True),
    ("id_rsa", 10, True),
    ("id_ed25519.pub", 10, True),
    ("gcp/credentials-prod.json", 10, True),
    ("cert.p12", 10, True),
    ("data.csv", snapshot.SIZE_LIMIT + 1, True),
    ("src/main.py", 10, False),
    ("environment.md", 10, False),
])
def test_secret_reason_flags_likely_secrets_and_large_files(path, size, skipped):
    assert bool(snapshot.secret_reason(path, size)) == skipped
