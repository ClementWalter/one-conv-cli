"""Codex thread locks: a running Codex holds a flock on the file, a killed one leaves it behind unlocked."""

import subprocess
import sys

import pytest

from one_conv.live import codex

THREAD = "01a21369-7288-7e51-ace0-5d55dd4484e9"
HOLD = "import fcntl, sys, time; f = open(sys.argv[1]); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)"


@pytest.fixture
def lock(tmp_path):
    path = tmp_path / f"{THREAD}.lock"
    path.touch()
    return path


@pytest.fixture
def holder(lock):
    """Another process holding the flock, as a running Codex does."""
    process = subprocess.Popen([sys.executable, "-c", HOLD, str(lock)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "locked"
    yield process
    process.kill()
    process.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="no advisory locks")
def test_lock_held_by_a_running_process(lock, holder):
    assert codex.lock_held(lock)


@pytest.mark.skipif(sys.platform == "win32", reason="no advisory locks")
def test_lock_left_by_a_killed_process_is_free(lock, holder):
    holder.kill()
    holder.wait()
    assert not codex.lock_held(lock)


def test_missing_lock_counts_as_held(tmp_path):
    assert codex.lock_held(tmp_path / "gone.lock")


def test_proc_locks_lists_flock_inodes():
    text = "1: FLOCK  ADVISORY  WRITE 1759 fd:01:5678 0 EOF\n2: POSIX  ADVISORY  WRITE 42 fd:01:9999 0 EOF\n"
    assert codex.flock_inodes(text) == {5678}


def test_unlocked_lock_file_is_not_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(codex.codex_rpc, "threads", lambda home: {})
    (tmp_path / "thread-writer-locks").mkdir()
    (tmp_path / "thread-writer-locks" / f"{THREAD}.lock").touch()
    assert codex.list_sessions() == []
