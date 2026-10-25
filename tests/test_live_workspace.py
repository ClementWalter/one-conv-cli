"""The spawner's workspace: a one-conv guide on disk that never overwrites the user's files, and fast-forward pulls."""

import subprocess

import pytest

from one_conv.live import workspace


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path):
    """A clone of a repository whose origin has one commit the clone has not pulled yet."""
    origin, local = tmp_path / "origin", tmp_path / "local"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "one")
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True, capture_output=True)
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "two")
    return origin, local


def head(folder):
    return subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True).stdout


def test_the_guide_is_written_in_an_empty_folder(tmp_path):
    workspace.write_guide(tmp_path, "mac")
    assert "read_conversation" in (tmp_path / "AGENTS.md").read_text()


def test_the_guide_names_the_node(tmp_path):
    workspace.write_guide(tmp_path, "cloudbox")
    assert "**cloudbox**" in (tmp_path / "AGENTS.md").read_text()


def test_claude_md_imports_the_guide(tmp_path):
    workspace.write_guide(tmp_path, "mac")
    assert "@AGENTS.md" in (tmp_path / "CLAUDE.md").read_text()


def test_a_guide_the_user_wrote_is_kept(tmp_path):
    (tmp_path / "AGENTS.md").write_text("mine")
    workspace.write_guide(tmp_path, "mac")
    assert (tmp_path / "AGENTS.md").read_text() == "mine"


def test_an_outdated_managed_guide_is_rewritten(tmp_path):
    (tmp_path / "AGENTS.md").write_text(workspace.MARKER + "\nold")
    workspace.write_guide(tmp_path, "mac")
    assert (tmp_path / "AGENTS.md").read_text() == workspace.guide("mac")


def test_a_clean_clone_is_fast_forwarded(clone):
    origin, local = clone
    workspace.pull(local)
    assert head(local) == head(origin)


def test_a_dirty_clone_is_not_pulled(clone):
    origin, local = clone
    (local / "tracked").write_text("x")
    git(local, "add", "tracked")
    assert workspace.pull(local) is False


def test_a_plain_folder_is_not_a_repository(tmp_path):
    assert workspace.pull(tmp_path) is False


def test_preparing_a_repository_writes_no_guide(clone, monkeypatch):
    monkeypatch.setattr(workspace, "CHECKOUT", clone[1])
    workspace.prepare(clone[1], "mac")
    assert not (clone[1] / "AGENTS.md").exists()


def test_preparing_a_plain_folder_writes_the_guide(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "pull", lambda repository: False)
    workspace.prepare(tmp_path, "mac")
    assert (tmp_path / "AGENTS.md").exists()
