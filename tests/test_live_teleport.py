"""`live teleport`: the target's checks and checkout, the bridge's request handling, and the origin's decisions.

Git runs for real in temporary repositories; tmux and the harness are stubbed as in test_live_spawn.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from one_conv.live import bridge, hubclient, inventory, names, provision, router, snapshot, spawn, teleport
from one_conv.live.cli import build_live_group
from one_conv.live.model import LiveSession

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
ORIGIN = LiveSession("claude", "c1", "/work", "lead")


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def executable(folder: Path, name: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def code(tmp_path):
    """A bare remote with a pushed `feature` branch, and a clone of it under the project root `projects`."""
    remote, work = tmp_path / "remote.git", tmp_path / "origin-clone"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", str(remote), str(work))
    (work / "a.txt").write_text("a")
    git(work, "add", ".")
    git(work, "commit", "-m", "one")
    git(work, "push", "origin", "main")
    git(work, "checkout", "-b", "feature")
    (work / "b.txt").write_text("b")
    git(work, "add", ".")
    git(work, "commit", "-m", "two")
    git(work, "push", "origin", "feature")
    clone = tmp_path / "projects" / "app"
    git(tmp_path, "clone", str(remote), str(clone))
    return SimpleNamespace(remote=remote, work=work, clone=clone, sha=git(work, "rev-parse", "HEAD"),
                           settings={provision.ACCEPT_KEY: True, provision.ROOTS_KEY: [str(tmp_path / "projects")],
                                     provision.WORKTREES_KEY: str(tmp_path / "worktrees")})


def repo_info(code, **extra) -> dict:
    return {"key": inventory.remote_key(str(code.remote)), "branch": "feature", "sha": code.sha, "subdir": "", **extra}


# finding and checking out the code ---------------------------------------------------------------

def test_find_repo_matches_the_remote_url(code):
    assert provision.find_repo(inventory.remote_key(str(code.remote)), [code.clone.parent]) == code.clone


def test_find_repo_ignores_other_remotes(code, tmp_path):
    assert provision.find_repo("github.com/someone/else", [code.clone.parent]) is None


def test_find_repo_prefers_a_main_checkout_over_a_worktree(code, tmp_path):
    git(code.clone, "worktree", "add", str(code.clone.parent / "aaa-worktree"), "-b", "side")
    assert provision.find_repo(inventory.remote_key(str(code.remote)), [code.clone.parent]) == code.clone


def test_a_missing_repo_is_reported_missing(code):
    _, rows = provision.locate({"repo": {"key": "github.com/x/y"}}, code.settings, None)
    assert rows[0]["status"] == "missing"


def test_a_given_cwd_that_does_not_exist_is_missing(code, tmp_path):
    _, rows = provision.locate({"repo": repo_info(code)}, code.settings, str(tmp_path / "nowhere"))
    assert rows[0]["status"] == "missing"


def test_a_commit_on_the_remote_branch_is_fetchable(code):
    assert provision.commit_status(str(code.clone), repo_info(code))[0] == "ok"


def test_a_commit_only_on_the_origin_is_missing(code):
    (code.work / "c.txt").write_text("c")
    git(code.work, "add", ".")
    git(code.work, "commit", "-m", "local")
    assert provision.commit_status(str(code.clone), repo_info(code, sha=git(code.work, "rev-parse", "HEAD")))[0] == "missing"


def test_a_checkout_already_at_the_commit_is_used_as_is(code):
    git(code.clone, "checkout", "feature")
    assert provision.checkout(str(code.clone), repo_info(code), "feature", "s1", code.settings)["dir"] == str(code.clone)


def test_a_checkout_on_another_branch_gets_a_worktree_on_the_origin_branch(code):
    place = provision.checkout(str(code.clone), repo_info(code), "feature", "s1", code.settings)
    assert (place["branch"], git(place["dir"], "rev-parse", "HEAD")) == ("feature", code.sha)


def test_a_worktree_never_switches_the_existing_checkout(code):
    provision.checkout(str(code.clone), repo_info(code), "feature", "s1", code.settings)
    assert git(code.clone, "symbolic-ref", "--short", "HEAD") == "main"


def test_a_dirty_checkout_on_the_branch_gets_its_own_teleport_branch(code):
    git(code.clone, "checkout", "feature")
    (code.clone / "a.txt").write_text("edited here")
    assert provision.checkout(str(code.clone), repo_info(code), "feature", "s1", code.settings)["branch"] == "teleport/s1"


def test_a_dirty_checkout_keeps_its_changes(code):
    git(code.clone, "checkout", "feature")
    (code.clone / "a.txt").write_text("edited here")
    provision.checkout(str(code.clone), repo_info(code), "feature", "s1", code.settings)
    assert (code.clone / "a.txt").read_text() == "edited here"


def test_a_commit_that_cannot_be_fetched_refuses_the_checkout(code):
    with pytest.raises(provision.ProvisionError, match="could not be fetched"):
        provision.checkout(str(code.clone), repo_info(code, sha="0" * 40), "feature", "s1", code.settings)


def test_a_wip_branch_brings_unpushed_commits(code):
    (code.work / "c.txt").write_text("c")
    git(code.work, "add", ".")
    git(code.work, "commit", "-m", "wip")
    git(code.work, "push", "origin", "HEAD:refs/heads/teleport/s1")
    sha = git(code.work, "rev-parse", "HEAD")
    place = provision.checkout(str(code.clone), repo_info(code, sha=sha), "teleport/s1", "s1", code.settings)
    assert git(place["dir"], "rev-parse", "HEAD") == sha


# the probe ----------------------------------------------------------------------------------------

@pytest.fixture
def machine(tmp_path, monkeypatch):
    """This machine's PATH holds `claude` and `git` only; its Claude config names the `one-conv` MCP server."""
    bin_dir = tmp_path / "bin"
    executable(bin_dir, "claude", "#!/bin/sh\necho '9.9.9 (Claude Code)'\n")
    executable(bin_dir, "tmux", "#!/bin/sh\nexit 0\n")
    os.symlink(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip(), bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text(json.dumps({"mcpServers": {"one-conv": {}}}))
    (tmp_path / "claude" / "skills" / "tdd").mkdir(parents=True)
    (tmp_path / "claude" / "skills" / "tdd" / "SKILL.md").write_text("x")
    (tmp_path / "home" / ".config" / "gh").mkdir(parents=True)
    (tmp_path / "home" / ".config" / "gh" / "hosts.yml").write_text("x")
    return tmp_path


@pytest.fixture
def probed(machine, code):
    """Probe rows keyed by (item, name) for an inventory mixing what this machine has and lacks."""
    wanted = {"harness": "claude", "cwd": "/elsewhere", "repo": repo_info(code), "clis": ["git", "wrangler"],
              "mcp": ["one-conv", "linear", "claude_ai_Slack"], "skills": ["tdd", "rescue"],
              "hints": ["~/.config/gh/hosts.yml", "~/.aws/credentials"]}
    answer = provision.probe(wanted, code.settings, None, "feature")
    return {(row["item"], row["name"]): row for row in answer["rows"]}


@pytest.mark.parametrize(("key", "status"), [
    (("harness", "claude"), "ok"),
    (("cli", "git"), "ok"),
    (("cli", "wrangler"), "missing"),
    (("mcp", "one-conv"), "ok"),
    (("mcp", "linear"), "missing"),
    (("mcp", "claude_ai_Slack"), "unknown"),
    (("skill", "tdd"), "ok"),
    (("skill", "rescue"), "missing"),
    (("hint", "~/.config/gh/hosts.yml"), "ok"),
    (("hint", "~/.aws/credentials"), "missing"),
])
def test_the_probe_reports_each_item(probed, key, status):
    assert probed[key]["status"] == status


def test_the_probe_reports_the_harness_version(probed):
    assert probed[("harness", "claude")]["detail"] == "9.9.9 (Claude Code)"


def test_the_probe_finds_the_repo_by_remote(probed, code):
    assert probed[("repo", inventory.remote_key(str(code.remote)))]["detail"] == str(code.clone)


def test_the_probe_checks_the_commit(probed, code):
    assert probed[("commit", f"feature@{code.sha[:12]}")]["status"] == "ok"


def test_a_missing_harness_is_reported_missing(machine, code):
    rows = provision.probe({"harness": "codex", "repo": repo_info(code)}, code.settings)["rows"]
    assert rows[0]["status"] == "missing"


@pytest.mark.parametrize(("config", "status"), [
    ('[mcp_servers.one-conv]\ncommand = "x"\n', "ok"),
    ('[mcp_servers.other]\ncommand = "x"\n', "missing"),
])
def test_codex_mcp_servers_come_from_its_config(machine, config, status):
    (machine / "codex").mkdir()
    (machine / "codex" / "config.toml").write_text(config)
    assert provision.mcp_status("codex", "one_conv") == status


# the request handler ------------------------------------------------------------------------------

def request_item(request: dict, created: float | None = None) -> dict:
    return {"delivery": uuid.uuid4().hex, "id": uuid.uuid4().hex, "to": "bridge", "from": "mac/claude:c1",
            "mode": "request", "text": json.dumps(request), "created": created or time.time()}


def test_a_machine_that_did_not_opt_in_refuses(code):
    outcome, result = provision.handle(request_item({"kind": "probe", "inventory": {}}), "box", {})
    assert "--accept-teleport" in result["error"]


def test_a_request_older_than_its_ttl_is_refused(code):
    outcome, result = provision.handle(request_item({"kind": "probe", "inventory": {}, "ttl": 5}, time.time() - 60),
                                       "box", code.settings)
    assert outcome == "refused"


def test_an_unknown_request_kind_is_refused(code):
    assert provision.handle(request_item({"kind": "shell"}), "box", code.settings)[0] == "refused"


def test_every_answered_request_is_logged(code):
    provision.handle(request_item({"kind": "probe", "inventory": {}}), "box", {})
    assert json.loads((Path(os.environ["ONE_CONV_LIVE_DIR"]) / "teleports.jsonl").read_text())["outcome"] == "refused"


# starting the session -----------------------------------------------------------------------------

@pytest.fixture
def tmux(monkeypatch, machine):
    """tmux starts are recorded; the started session registers at once, working where it was started."""
    started = []
    monkeypatch.setattr(spawn, "start", lambda name, cwd, command: started.append((name, cwd, command)))
    monkeypatch.setattr(spawn, "tmux_taken", lambda name: any(row[0] == name for row in started))
    monkeypatch.setattr(spawn, "screen", lambda name, lines=15: "waiting for trust prompt")
    monkeypatch.setattr(router.ADAPTERS["claude"], "list_sessions",
                        lambda: [LiveSession("claude", "new1", started[-1][1])] if started else [])
    monkeypatch.setattr(router, "list_all", lambda harness=None: router.ADAPTERS["claude"].list_sessions())
    return started


def spawn_request(code, **extra) -> dict:
    return {"kind": "spawn", "origin": "mac/claude:c1", "short": "s1", "fetch": "feature", "mode": "yolo",
            "seed": "[turn 1 of 1] user:\nfix the bug", "summary": "fix the bug", "name": "lead",
            "inventory": {"harness": "claude", "model": "claude-opus-5-5", "repo": repo_info(code)}, **extra}


@pytest.fixture
def started(tmux, code):
    return provision.start_session(spawn_request(code), "box", code.settings, timeout=1)


def test_the_new_session_is_addressed_on_the_target_node(started):
    assert started["address"] == "box/claude:new1"


def test_the_harness_starts_on_the_origin_model(started, tmux):
    argv = tmux[0][2].argv
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"


def test_the_harness_starts_in_the_origin_mode(started, tmux):
    assert "--dangerously-skip-permissions" in tmux[0][2].argv


def test_the_brief_asks_for_the_confirmation_first(started, tmux):
    assert 'FIRST, before anything else, send mac/claude:c1 exactly one message' in tmux[0][2].argv[-1]


def test_the_brief_carries_the_seed(started, tmux):
    assert tmux[0][2].argv[-1].endswith("fix the bug")


def test_the_origin_name_moves_to_the_new_session(started):
    assert names.address_for("lead") == "claude:new1"


def test_the_spawn_is_recorded_for_live_stop(started):
    assert spawn.load_registry()[started["tmux"]].address == "claude:new1"


def test_a_session_that_never_registers_reports_its_screen(tmux, code, monkeypatch):
    monkeypatch.setattr(router.ADAPTERS["claude"], "list_sessions", lambda: [])
    with pytest.raises(provision.ProvisionError) as raised:
        provision.start_session(spawn_request(code), "box", code.settings, timeout=0)
    assert raised.value.screen == "waiting for trust prompt"


@pytest.fixture
def provisioned(tmux, code):
    """A spawn carrying a provisioning brief; returns the first prompt the harness got."""
    provision.start_session(spawn_request(code, provisioning="# Provisioning box\n## cli `wa` (missing)"), "box",
                            code.settings, timeout=1)
    return tmux[0][2].argv[-1]


def test_the_brief_is_step_0_of_the_first_prompt(provisioned):
    assert "STEP 0, right after that message: provision this machine." in provisioned


def test_the_first_prompt_carries_the_brief(provisioned):
    assert "<provisioning-brief>\n# Provisioning box\n## cli `wa` (missing)\n</provisioning-brief>" in provisioned


def test_the_first_prompt_asks_for_one_note_to_the_user(provisioned):
    assert 'send ONE message with send_message, to="user" and tags=["teleport"]' in provisioned


def test_the_brief_is_saved_on_the_target(provisioned):
    assert (Path(os.environ["ONE_CONV_LIVE_DIR"]) / "teleport-briefs" / "s1.md").read_text().startswith("# Provisioning box")


def test_without_a_brief_there_is_no_step_0(started, tmux):
    assert "STEP 0" not in tmux[0][2].argv[-1]


def test_a_snapshot_is_checked_out_on_its_teleport_branch(code):
    (code.work / "c.txt").write_text("c")
    git(code.work, "add", ".")
    git(code.work, "commit", "-m", "wip(teleport): s1")
    git(code.work, "push", "origin", "HEAD:refs/heads/teleport/s1")
    sha = git(code.work, "rev-parse", "HEAD")
    place = provision.checkout(str(code.clone), repo_info(code, sha=sha), "teleport/s1", "s1", code.settings, "teleport/s1")
    assert (place["branch"], git(place["dir"], "rev-parse", "HEAD")) == ("teleport/s1", sha)


def test_a_missing_repo_is_cloned_for_the_session(tmux, code, tmp_path):
    settings = {**code.settings, provision.ROOTS_KEY: [str(tmp_path / "empty-root")]}
    (tmp_path / "empty-root").mkdir()
    request = spawn_request(code)
    request["inventory"]["repo"]["remote"] = str(code.remote)
    provision.start_session(request, "box", settings, timeout=1)
    assert git(tmp_path / "empty-root" / "remote", "rev-parse", "HEAD") == git(code.remote, "rev-parse", "main")


def test_a_cwd_of_another_repository_is_refused_without_force(tmux, code, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", str(other))
    with pytest.raises(provision.ProvisionError, match="pass --force"):
        provision.start_session(spawn_request(code, cwd=str(other)), "box", code.settings, timeout=1)


def test_a_missing_tmux_refuses_the_spawn(tmux, code, monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "tmux" else real(name, *a, **k))
    with pytest.raises(provision.ProvisionError, match="tmux is not installed"):
        provision.start_session(spawn_request(code), "box", code.settings, timeout=1)


@pytest.fixture
def npm(machine):
    """An `npm` on PATH whose global install drops a `codex` launcher next to it."""
    executable(machine / "bin", "npm", f"#!/bin/sh\nprintf '#!/bin/sh\\n' > {machine}/bin/codex && /bin/chmod +x {machine}/bin/codex\n")
    return machine


def test_the_probe_offers_to_install_a_missing_harness(npm):
    assert provision.probe({"harness": "codex"}, {})["rows"][0]["install"] == "npm install -g @openai/codex"


def test_a_missing_harness_is_installed_before_the_spawn(npm):
    assert provision.ensure_harness("codex", "box") == "installed codex with `npm install -g @openai/codex`"


def test_a_harness_without_a_silent_install_refuses(machine):
    with pytest.raises(provision.ProvisionError, match="cannot be installed without a person"):
        provision.ensure_harness("omp", "box")


# the bridge answers requests through a real relay -------------------------------------------------

def serve_app(app):
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)

    def stop():
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    return f"http://127.0.0.1:{sock.getsockname()[1]}", stop


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """A Python relay with nodes mac (session claude:c1) and box, box's bridge answering requests with `run`."""
    pytest.importorskip("uvicorn")
    from one_conv.live.hub import Hub, application
    monkeypatch.setattr(router, "list_all", lambda harness=None: [])
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve_app(application(state))
    mac = hubclient.HubClient(url, state.add_token("mac"), "mac")
    mac.request("PUT", "/v1/sessions", {"sessions": [{"address": "claude:c1"}]})
    calls = []

    def run(item):
        calls.append(item)
        return "accepted", {"rows": [{"item": "cli", "name": "git", "status": "ok", "detail": ""}]}
    box = bridge.Bridge(hubclient.HubClient(url, state.add_token("box"), "box"), "box",
                        bridge.Ingress(tmp_path / "ingress.db"), requests=bridge.Requests("box", run))
    yield SimpleNamespace(mac=mac, box=box, calls=calls)
    stop()


def answered(relay, message_id: str, steps: int = 50) -> dict:
    """The request's status once box's bridge stepped until it answered it."""
    for _ in range(steps):
        relay.box.step(wait=0)
        row = relay.mac.request("GET", f"/v1/messages/{message_id}")["recipients"][0]
        if row["outcome"] in teleport.FINAL:
            return row
        time.sleep(0.05)
    raise AssertionError("box's bridge never answered")


def test_the_bridge_answers_a_request_with_its_result(relay):
    message_id = teleport.post_request(relay.mac, "box", "claude:c1", {"kind": "probe"})
    assert answered(relay, message_id)["result"]["rows"][0]["name"] == "git"


def test_the_bridge_runs_a_request_once(relay):
    message_id = teleport.post_request(relay.mac, "box", "claude:c1", {"kind": "probe"})
    answered(relay, message_id)
    relay.box.step(wait=0)
    assert len(relay.calls) == 1


def test_a_request_claimed_before_a_restart_is_answered_unconfirmed(relay, tmp_path):
    message_id = teleport.post_request(relay.mac, "box", "claude:c1", {"kind": "probe"})
    item = relay.box.client.request("GET", "/v1/inbox?wait=0")["messages"][0]
    relay.box.ingress.claim(item)
    relay.box.handle(item)
    assert relay.mac.request("GET", f"/v1/messages/{message_id}")["recipients"][0]["outcome"] == "unconfirmed"


@pytest.fixture
def stepping(relay):
    """box's bridge steps in the background until the test ends."""
    done = threading.Event()

    def loop():
        while not done.is_set():
            relay.box.step(wait=0.1)
    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    yield relay
    done.set()
    worker.join(timeout=5)


def test_ask_returns_the_answer(stepping):
    assert teleport.ask(stepping.mac, "box", "claude:c1", {"kind": "probe"}, 10)["rows"][0]["status"] == "ok"


def test_a_session_started_after_the_last_publication_can_send_at_once(relay, monkeypatch):
    monkeypatch.setattr(router, "list_all", lambda harness=None: [LiveSession("omp", "fresh")])
    message = {"id": uuid.uuid4().hex, "to": "box", "from": "omp:fresh", "mode": "request", "text": "{}"}
    assert router._submit_published(relay.mac, message)["id"] == message["id"]


def test_an_unanswered_request_times_out_naming_the_node(relay):
    with pytest.raises(teleport.TeleportError, match="box did not answer"):
        teleport.ask(relay.mac, "box", "claude:c1", {"kind": "probe"}, 0, sleep=lambda _: None)


# the origin's decisions ---------------------------------------------------------------------------

def found(**repo) -> inventory.Inventory:
    checkout = inventory.Repo(root="/work", remote="git@github.com:o/app.git", key="github.com/o/app",
                              branch="feature", sha="a" * 40, **repo)
    return inventory.Inventory(harness="claude", cwd="/work", repo=checkout)


def rows(*statuses, **extra) -> list[dict]:
    return [{"item": item, "name": name, "status": status, "detail": "", **extra} for item, name, status in statuses]


@pytest.mark.parametrize(("checked", "force", "blocked"), [
    (rows(("cli", "git", "ok")), False, False),
    (rows(("cli", "wrangler", "missing")), False, False),
    (rows(("mcp", "linear", "unknown")), False, False),
    (rows(("repo", "github.com/o/app", "missing")), False, False),
    (rows(("hint", "~/.config/gh/hosts.yml", "missing")), False, False),
    (rows(("tmux", "tmux", "missing")), True, True),
    (rows(("harness", "omp", "missing")), True, True),
    (rows(("harness", "claude", "missing"), install="npm install -g @anthropic-ai/claude-code"), False, False),
    (rows(("folder", "/srv/other", "dubious")), False, True),
    (rows(("folder", "/srv/other", "dubious")), True, False),
])
def test_only_tmux_an_uninstallable_harness_or_a_dubious_folder_block(checked, force, blocked):
    assert bool(teleport.blockers(checked, force)) == blocked


@pytest.mark.parametrize(("repo", "checked", "needed"), [
    ({}, [], False),
    ({"changed": 1}, [], True),
    ({"untracked": 1}, [], True),
    ({"unpushed": 2}, [], True),
    ({}, rows(("commit", "feature@aaa", "missing")), True),
])
def test_a_snapshot_is_needed_only_for_work_the_target_cannot_fetch(repo, checked, needed):
    assert teleport.needs_snapshot(found(**repo), checked) == needed


@pytest.mark.parametrize(("harness", "explicit", "detected", "mode"), [
    ("claude", None, "yolo", "yolo"),
    ("claude", "ask", "yolo", "ask"),
    ("claude", None, "", "ask"),
    ("omp", None, "auto", "ask"),
])
def test_the_mode_is_the_flag_else_the_sessions_own(harness, explicit, detected, mode):
    assert teleport.choose_mode(harness, explicit, detected) == mode


def test_a_mode_the_harness_lacks_is_refused_when_asked_for():
    with pytest.raises(teleport.TeleportError):
        teleport.choose_mode("omp", "auto", "")


@pytest.fixture
def origin(monkeypatch):
    """teleport.run with the target's answers and the snapshot stubbed: probe rows, then a started session."""
    asked, snapshots = [], []
    answers = {"probe": {"rows": rows(("cli", "git", "ok")), "dir": "/srv/app", "platform": "linux"},
               "spawn": {"address": "box/claude:new1", "dir": "/srv/app", "branch": "feature", "sha": "a" * 40}}

    def ask(hub, node, sender, request, timeout, **_):
        asked.append(request)
        answer = answers[request["kind"]]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def take(root, short, push=True):
        snapshots.append(root)
        return snapshot.Snapshot(branch=f"teleport/{short}", commit="b" * 40, files=2, skipped=[])
    monkeypatch.setattr(teleport, "ask", ask)
    monkeypatch.setattr(teleport, "sync_archive", lambda *a, **k: "")
    monkeypatch.setattr(snapshot, "take", take)
    monkeypatch.setattr(snapshot, "plan", lambda root: (["a.txt", "b.txt"], [{"path": ".env", "reason": "looks like a secret"}]))
    hub = SimpleNamespace(node="mac", url="http://127.0.0.1:1", token="t")
    return SimpleNamespace(asked=asked, answers=answers, hub=hub, snapshots=snapshots)


def test_a_check_stops_after_the_probe(origin):
    teleport.run("box", ORIGIN, {}, found(), check_only=True, hub=origin.hub)
    assert [request["kind"] for request in origin.asked] == ["probe"]


def test_a_check_returns_the_brief_for_what_the_target_lacks(origin):
    origin.answers["probe"]["rows"] = rows(("cli", "git", "missing"))
    assert "## cli `git` (missing)" in teleport.run("box", ORIGIN, {}, found(), check_only=True, hub=origin.hub)["brief"]


def test_a_check_saves_the_brief_under_the_live_folder(origin):
    origin.answers["probe"]["rows"] = rows(("cli", "git", "missing"))
    report = teleport.run("box", ORIGIN, {}, found(), check_only=True, hub=origin.hub)
    assert Path(report["brief_path"]).read_text() == report["brief"]


def test_a_check_plans_the_snapshot_without_taking_it(origin):
    teleport.run("box", ORIGIN, {}, found(changed=1), check_only=True, hub=origin.hub)
    assert origin.snapshots == []


def test_a_teleport_reports_the_new_address(origin):
    assert teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)["address"] == "box/claude:new1"


def test_a_teleport_names_the_origin_by_its_full_route(origin):
    teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)
    assert origin.asked[1]["origin"] == "mac/claude:c1"


def test_a_teleport_tells_the_agent_to_stop(origin):
    assert "Stop working now" in teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)["next"]


def test_missing_tools_still_teleport(origin):
    origin.answers["probe"]["rows"] = rows(("cli", "wrangler", "missing"))
    assert teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)["status"] == "teleported"


def test_the_brief_ships_with_the_spawn(origin):
    origin.answers["probe"]["rows"] = rows(("cli", "wrangler", "missing"))
    teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)
    assert "## cli `wrangler` (missing)" in origin.asked[1]["provisioning"]


def test_a_missing_tmux_never_spawns(origin):
    origin.answers["probe"]["rows"] = rows(("tmux", "tmux", "missing"))
    teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)
    assert [request["kind"] for request in origin.asked] == ["probe"]


def test_a_blocked_teleport_still_returns_the_brief(origin):
    origin.answers["probe"]["rows"] = rows(("tmux", "tmux", "missing"))
    assert "sudo apt-get install tmux" in teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)["brief"]


def test_uncommitted_work_is_snapshotted(origin):
    assert teleport.run("box", ORIGIN, {}, found(changed=1), hub=origin.hub)["snapshot"]["branch"].startswith("teleport/")


def test_the_target_fetches_the_snapshot_branch(origin):
    teleport.run("box", ORIGIN, {}, found(changed=1), hub=origin.hub)
    assert origin.asked[1]["fetch"] == origin.asked[1]["wip_branch"]


def test_the_target_checks_out_the_snapshot_commit(origin):
    teleport.run("box", ORIGIN, {}, found(changed=1), hub=origin.hub)
    assert origin.asked[1]["inventory"]["repo"]["sha"] == "b" * 40


def test_a_clean_pushed_tree_takes_no_snapshot(origin):
    teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)
    assert origin.snapshots == []


def test_the_snapshot_is_logged(origin):
    teleport.run("box", ORIGIN, {}, found(changed=1), hub=origin.hub)
    rows_logged = [json.loads(line) for line in (Path(os.environ["ONE_CONV_LIVE_DIR"]) / "teleports.jsonl").read_text().splitlines()]
    assert rows_logged[-1]["snapshot"]["files"] == 2


def test_the_name_travels_with_the_spawn_request(origin):
    names.set_name("lead", ORIGIN.address, lambda address: True)
    teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)
    assert origin.asked[1]["name"] == "lead"


def test_the_origin_releases_its_name(origin):
    names.set_name("lead", ORIGIN.address, lambda address: True)
    teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)
    assert names.address_for("lead") is None


def test_a_failed_spawn_gives_the_name_back(origin):
    names.set_name("lead", ORIGIN.address, lambda address: True)
    origin.answers["spawn"] = teleport.TeleportError("tmux missing")
    with pytest.raises(teleport.TeleportError):
        teleport.run("box", ORIGIN, {}, found(), hub=origin.hub)
    assert names.address_for("lead") == ORIGIN.address


def test_teleporting_to_this_machine_is_refused(origin):
    with pytest.raises(teleport.TeleportError, match="is this machine"):
        teleport.run("mac", ORIGIN, {}, found(), hub=origin.hub)


# the command ---------------------------------------------------------------------------------------

@pytest.fixture
def command(monkeypatch, origin):
    monkeypatch.setattr(router, "whoami", lambda *a, **kw: ORIGIN)
    monkeypatch.setattr(hubclient, "configured", lambda: origin.hub)
    group = build_live_group(["one-conv"], lambda address: {"blocks": [], "seed": "s", "summary": "x"})
    return lambda *args: CliRunner().invoke(group, ["teleport", *args])


def test_without_yes_the_command_only_checks(command, origin):
    command("box")
    assert [request["kind"] for request in origin.asked] == ["probe"]


def test_the_check_prints_the_table(command):
    assert "ok       cli      git" in command("box", "--check").output


def test_the_check_prints_the_brief(command, origin):
    origin.answers["probe"]["rows"] = rows(("cli", "git", "missing"))
    assert "## cli `git` (missing)" in command("box", "--check").output


def test_a_refused_teleport_exits_non_zero(command, origin):
    origin.answers["probe"]["rows"] = rows(("tmux", "tmux", "missing"))
    assert command("box", "--yes").exit_code == 1


def test_the_json_report_carries_the_new_address(command):
    assert json.loads(command("box", "--yes", "--json").output)["address"] == "box/claude:new1"


def test_serve_saves_the_opt_in(tmp_path):
    from one_conv.live.bridge_cli import add_bridge_commands
    import click
    group = click.Group("live")
    add_bridge_commands(group, ["one-conv"])
    CliRunner().invoke(group, ["serve", "--accept-teleport"])
    assert bridge.settings()[provision.ACCEPT_KEY] is True


def test_saving_the_connection_keeps_the_opt_in():
    bridge.save_accept_teleport(True)
    bridge.save_settings("https://relay.example", "box", 8789, "token")
    assert bridge.settings()[provision.ACCEPT_KEY] is True


def test_sys_executable_runs_the_cli():
    # The MCP tool runs `live teleport` through the same launcher as the readers.
    assert subprocess.run([sys.executable, "-O", str(Path(__file__).resolve().parents[1] / "one_conv_cli.py"),
                           "live", "teleport", "--help"], capture_output=True, text=True, timeout=60).returncode == 0
