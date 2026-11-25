"""Update notices: what is installed, what GitHub has, the daily cache and how often it is shown."""

import json
import subprocess
import urllib.error

import pytest

from one_conv import update_check
from one_conv.update_check import Status

LOCAL = "a" * 40
NEW = "b" * 40
DAY = 24 * 3600


def _commit(sha, subject, parents=1):
    return {"sha": sha, "commit": {"message": f"{subject}\n\nbody"}, "parents": [{}] * parents}


COMPARE = {"ahead_by": 3, "commits": [_commit("c1", "Add spawn modes"), _commit("c2", "Merge pull request #148", 2),
                                      _commit(NEW, "Add update notices")]}


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.delenv(update_check.DISABLE_ENV, raising=False)


@pytest.fixture
def checkout(tmp_path):
    """A git checkout with one commit, standing in for the one-conv root."""
    root = tmp_path / "one-conv-cli"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    return root


@pytest.fixture
def extension(tmp_path):
    """The Desktop extension layout: <extension>/manifest.json and <extension>/cli/<repo>/."""
    root = tmp_path / "ext" / "cli" / "one-conv-cli"
    root.mkdir(parents=True)
    (tmp_path / "ext" / "manifest.json").write_text(json.dumps({"version": "0.6.0"}))
    return root


def _github(compare=COMPARE, release="v0.7.0"):
    calls = []

    def get(url, timeout):
        calls.append(url)
        return compare if "/compare/" in url else {"tag_name": release}
    get.calls = calls
    return get


def test_checkout_is_git(checkout):
    assert update_check.installed(checkout).kind == "git"


def test_checkout_version_is_head(checkout):
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert update_check.installed(checkout).local == head.strip()


def test_extension_version_is_its_manifest(extension):
    assert update_check.installed(extension) == Status("extension", "0.6.0")


def test_plain_copy_has_no_version(tmp_path):
    assert update_check.installed(tmp_path).kind == "copy"


def test_git_behind_counts_main_commits():
    assert update_check.fetch(Status("git", LOCAL), get=_github()).behind == 3


def test_git_subjects_skip_merges_newest_first():
    assert update_check.fetch(Status("git", LOCAL), get=_github()).subjects == ["Add update notices", "Add spawn modes"]


def test_git_compares_head_with_main():
    get = _github()
    update_check.fetch(Status("git", LOCAL), get=get)
    assert get.calls == [f"{update_check.API}/compare/{LOCAL}...main"]


def test_git_up_to_date_is_not_outdated():
    assert not update_check.fetch(Status("git", LOCAL), get=_github({"ahead_by": 0, "commits": []})).outdated


def test_extension_older_than_release_is_outdated():
    assert update_check.fetch(Status("extension", "0.6.0"), get=_github(release="v0.7.0")).outdated


def test_extension_newer_than_release_is_not_outdated():
    assert not update_check.fetch(Status("extension", "0.6.0"), get=_github(release="v0.1.0")).outdated


def test_unreachable_github_is_not_outdated():
    def get(url, timeout):
        raise urllib.error.URLError("offline")
    assert not update_check.fetch(Status("git", LOCAL), get=get).outdated


def test_check_reuses_a_fresh_cache(tmp_path, checkout):
    update_check.check(tmp_path, root=checkout, get=_github())
    get = _github()
    update_check.check(tmp_path, root=checkout, get=get)
    assert get.calls == []


def test_check_refreshes_a_day_old_cache(tmp_path, checkout):
    update_check.check(tmp_path, root=checkout, get=_github())
    get = _github()
    update_check.check(tmp_path, root=checkout, get=get, now=lambda: update_check.time.time() + DAY + 1)
    assert len(get.calls) == 1


def test_check_skips_github_for_a_copy(tmp_path):
    get = _github()
    update_check.check(tmp_path / "state", root=tmp_path, get=get)
    assert get.calls == []


def test_git_notice_names_the_pull_command(checkout):
    status = Status("git", LOCAL, NEW, 3, ["Add update notices"])
    assert f"git -C {checkout} pull --ff-only" in update_check.notice(status, checkout)


def test_extension_notice_links_the_release():
    assert update_check.EXTENSION_URL in update_check.notice(Status("extension", "0.6.0", "0.7.0", 1))


def test_up_to_date_has_no_notice():
    assert update_check.notice(Status("git", LOCAL, LOCAL)) == ""


def test_cli_notice_shows_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "ROOT", tmp_path)
    monkeypatch.setattr(update_check, "installed", lambda root=None: Status("git", LOCAL))
    update_check.cli_notice(tmp_path, get=_github())
    assert update_check.cli_notice(tmp_path, get=_github()) == ""


def test_cli_notice_shows_again_after_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "installed", lambda root=None: Status("git", LOCAL))
    update_check.cli_notice(tmp_path, get=_github())
    later = update_check.time.time() + DAY + 1
    assert update_check.cli_notice(tmp_path, get=_github(), now=lambda: later).startswith("one-conv update available")


def test_session_notice_shows_every_session(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "installed", lambda root=None: Status("git", LOCAL))
    update_check.cli_notice(tmp_path, get=_github())
    assert update_check.session_notice(tmp_path, get=_github()).startswith("one-conv update available")


def test_disabled_check_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv(update_check.DISABLE_ENV, "1")
    monkeypatch.setattr(update_check, "installed", lambda root=None: Status("git", LOCAL))
    assert update_check.session_notice(tmp_path, get=_github()) == ""


def test_pull_refuses_another_branch(checkout):
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "-b", "feat/x"], check=True)
    with pytest.raises(ValueError, match="on feat/x, not main"):
        update_check.pull(checkout)


def test_pull_refuses_uncommitted_changes(checkout):
    (checkout / "f").write_text("x")
    subprocess.run(["git", "-C", str(checkout), "add", "f"], check=True)
    with pytest.raises(ValueError, match="uncommitted changes"):
        update_check.pull(checkout)
