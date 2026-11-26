"""`live teleport` end to end: two real bridges on a local relay move a session from `mac` to `box`.

mac runs a stand-in omp session (a registry file naming a `sleep` process, and an omp transcript
that ran `wacli send`, a CLI only mac has) working in a clone of a local bare repository with
uncommitted work and a `.env`. box has another clone under its project root and opted in with
`live serve --accept-teleport`. tmux on box is a stand-in that records the first prompt, registers
a new omp session where it was started and, like the briefed agent, sends the brief's confirmation
back to the origin with `live send`. Every path is under the test's own directory.
"""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from test_archive_everywhere import history_env

uvicorn = pytest.importorskip("uvicorn")

ROOT = Path(__file__).resolve().parents[1]
ORIGIN_ID = "origin-1"
DEADLINE = 90
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

FAKE_TMUX = '''#!{python}
"""Stands in for tmux: new-session registers an omp session in its folder that sends the brief's confirmation."""
import json, os, re, subprocess, sys, uuid
from pathlib import Path

state = Path(os.environ["FAKE_TMUX_DIR"])
state.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]


def target():
    return args[args.index("-t") + 1].lstrip("=").rstrip(":")


if args[0] == "has-session":
    sys.exit(0 if (state / target()).exists() else 1)
if args[0] in ("capture-pane", "kill-session"):
    sys.exit(0)
if args[0] != "new-session":
    sys.exit(1)
name, cwd, brief = args[args.index("-s") + 1], args[args.index("-c") + 1], args[-1]
ident = "teleported-" + uuid.uuid4().hex[:8]
sleeper = subprocess.Popen(["sleep", "600"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(state / name).write_text(str(sleeper.pid))
(state / f"{{name}}.prompt").write_text(brief)
registry = Path(os.environ["ONE_CONV_LIVE_DIR"]) / "omp"
registry.mkdir(parents=True, exist_ok=True)
(registry / f"{{ident}}.json").write_text(json.dumps({{"sessionId": ident, "pid": sleeper.pid, "cwd": cwd}}))
origin = re.search(r'to="([^"]+)"', brief)[1]
text = re.search(r"^teleported: now .*$", brief, re.M)[0].replace("<your address>", f"omp:{{ident}}")
subprocess.Popen([sys.executable, os.environ["ONE_CONV_CLI"], "live", "send", origin, text, "--from", f"omp:{{ident}}",
                  "--no-reply", "--yes"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
'''


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def until(check, what: str):
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def node_env(root: Path, extra: dict) -> dict:
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("ONE_CONV_HUB_", "ONE_CONV_ARCHIVE_", "ONE_CONV_PROJECT_", "ONE_CONV_TELEPORT_"))}
    return {**env, **history_env(root), "ONE_CONV_ARCHIVE_SYNC_MINUTES": "0", **extra}


def write_origin(env: dict, cwd: Path) -> subprocess.Popen:
    """The stand-in omp session on mac: a transcript that ran `git status`, and a registry naming a live process."""
    sessions = Path(env["OMP_HOME"]) / "agent" / "sessions" / "app"
    sessions.mkdir(parents=True)
    rows = [{"type": "session", "id": ORIGIN_ID, "cwd": str(cwd), "timestamp": "2026-01-01T10:00:00Z"},
            {"type": "message", "timestamp": "2026-01-01T10:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "fix the flaky parser test"}]}},
            {"type": "message", "timestamp": "2026-01-01T10:00:02Z",
             "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash",
                                                           "arguments": {"command": "git status"}}]}},
            {"type": "message", "timestamp": "2026-01-01T10:00:02Z",
             "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash",
                                                           "arguments": {"command": "wacli send parser-team"}}]}},
            {"type": "message", "timestamp": "2026-01-01T10:00:03Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "The parser test is flaky."}]}}]
    (sessions / f"2026_{ORIGIN_ID}.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    stub = subprocess.Popen(["sleep", "600"])
    registry = Path(env["ONE_CONV_LIVE_DIR"]) / "omp" / f"{ORIGIN_ID}.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"sessionId": ORIGIN_ID, "pid": stub.pid, "cwd": str(cwd), "title": "parser"}))
    return stub


def published(url: str, token: str) -> set[str]:
    request = Request(url + "/v1/sessions", headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=10) as response:
        return {row["address"] for row in json.loads(response.read())["sessions"]}


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    from one_conv.live.hub import Hub, application
    folder = tmp_path_factory.mktemp("relay")
    hub = Hub(str(folder / "relay.db"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application(hub, str(folder / "archive.db")), log_level="error",
                                           access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield SimpleNamespace(url=f"http://127.0.0.1:{sock.getsockname()[1]}", hub=hub)
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


@pytest.fixture(scope="module")
def teleported(relay, tmp_path_factory):
    """mac's stand-in session teleports to box; returns the report, mac's env and box's clone."""
    root = tmp_path_factory.mktemp("teleport")
    remote, mac_clone, box_clone = root / "remote.git", root / "mac-code" / "app", root / "box-code" / "app"
    git(root, "init", "--bare", "-b", "main", str(remote))
    git(root, "clone", str(remote), str(mac_clone))
    (mac_clone / "parser.py").write_text("print('parse')\n")
    git(mac_clone, "add", ".")
    git(mac_clone, "commit", "-m", "parser")
    git(mac_clone, "push", "origin", "main")
    git(root, "clone", str(remote), str(box_clone))
    (mac_clone / "parser.py").write_text("print('parse, fixed')\n")
    (mac_clone / "notes.md").write_text("flaky because of the clock\n")
    (mac_clone / ".env").write_text("TOKEN=never-travels\n")
    mac_status = git(mac_clone, "status", "--porcelain")
    fake_bin, mac_bin = root / "bin", root / "mac-bin"
    executable(mac_bin / "wacli", "#!/bin/sh\n")
    executable(fake_bin / "tmux", FAKE_TMUX.replace("{python}", sys.executable).replace("{{", "{").replace("}}", "}"))
    executable(fake_bin / "omp", "#!/bin/sh\necho 'omp 0.0-test'\n")
    path = f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
    mac = node_env(root / "mac", {"PATH": f"{mac_bin}{os.pathsep}{path}"})
    box = node_env(root / "box", {"PATH": path, "FAKE_TMUX_DIR": str(root / "tmux"),
                                  "ONE_CONV_CLI": str(ROOT / "one_conv_cli.py"),
                                  "ONE_CONV_PROJECT_ROOTS": str(root / "box-code"),
                                  "ONE_CONV_TELEPORT_WORKTREES": str(root / "box-worktrees")})
    stub = write_origin(mac, mac_clone)
    tokens = {"mac": relay.hub.add_token("mac"), "box": relay.hub.add_token("box")}
    bridges = []
    for name, env, extra in (("mac", mac, []), ("box", box, ["--accept-teleport"])):
        process = subprocess.Popen([sys.executable, str(ROOT / "one_conv_cli.py"), "live", "serve", "--hub", relay.url,
                                    "--node", name, "--token-stdin", *extra], env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=(root / f"{name}-bridge.log").open("w"), text=True)
        process.stdin.write(tokens[name])
        process.stdin.close()
        bridges.append(process)
    try:
        until(lambda: f"mac/omp:{ORIGIN_ID}" in published(relay.url, tokens["box"]), "mac's stand-in session")
        until(lambda: (Path(box["ONE_CONV_LIVE_DIR"]) / "bridge" / "hub.json").exists(), "box's bridge")
        result = subprocess.run([sys.executable, str(ROOT / "one_conv_cli.py"), "live", "teleport", "box",
                                 "--from", f"omp:{ORIGIN_ID}", "--yes", "--json"], env=mac, capture_output=True,
                                text=True, timeout=300, check=False)
        assert result.stdout, result.stderr
        inbox = Path(mac["ONE_CONV_LIVE_DIR"]) / "inbox" / "omp" / ORIGIN_ID
        confirmation = until(lambda: sorted(inbox.glob("*.json")), "the confirmation on mac")[0]
        report = json.loads(result.stdout)
        yield SimpleNamespace(report=report, confirmation=json.loads(confirmation.read_text())["text"],
                              box_clone=box_clone, head=git(mac_clone, "rev-parse", "HEAD"), box=box,
                              prompt=next((root / "tmux").glob("*.prompt")).read_text(),
                              mac_status=(mac_status, git(mac_clone, "status", "--porcelain")),
                              worktree=Path(report["dir"]))
    finally:
        for process in bridges:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)
        stub.kill()
        stub.wait()
        for started in (root / "tmux").glob("*"):
            try:
                os.kill(int(started.read_text()), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass


def test_the_teleport_reports_success(teleported):
    assert teleported.report["status"] == "teleported"


def test_the_new_session_lives_on_box(teleported):
    assert teleported.report["address"].startswith("box/omp:teleported-")


def test_the_snapshot_is_checked_out_in_a_new_worktree(teleported):
    assert teleported.worktree.parent.parent.name == "box-worktrees"


def test_the_new_session_is_on_the_snapshot_branch(teleported):
    assert teleported.report["branch"] == teleported.report["snapshot"]["branch"]


def test_the_snapshot_sits_on_the_origin_head(teleported):
    assert git(teleported.worktree, "rev-parse", "HEAD^") == teleported.head


def test_the_uncommitted_work_reaches_box(teleported):
    assert (teleported.worktree / "notes.md").read_text() == "flaky because of the clock\n"


def test_the_secret_file_stays_behind(teleported):
    assert not (teleported.worktree / ".env").exists()


def test_the_report_lists_what_stayed_behind(teleported):
    assert teleported.report["snapshot"]["skipped"] == [{"path": ".env", "reason": "looks like a secret"}]


def test_the_origin_checkout_is_untouched(teleported):
    assert teleported.mac_status[0] == teleported.mac_status[1]


def test_the_brief_names_the_cli_box_lacks(teleported):
    assert "## cli `wacli` (missing)" in teleported.report["brief"]


def test_the_brief_says_why_the_session_needs_it(teleported):
    assert "- Why: ran `wacli send`." in teleported.report["brief"]


def test_the_continuation_prompt_has_step_0(teleported):
    assert "STEP 0, right after that message: provision this machine." in teleported.prompt


def test_the_continuation_prompt_carries_the_brief(teleported):
    assert "## cli `wacli` (missing)" in teleported.prompt


def test_the_brief_is_saved_on_box(teleported):
    assert list((Path(teleported.box["ONE_CONV_LIVE_DIR"]) / "teleport-briefs").glob("*.md"))


def test_the_origin_receives_the_confirmation(teleported):
    assert f"teleported: now {teleported.report['address']} in {teleported.worktree}" in teleported.confirmation


def test_the_confirmation_names_the_task(teleported):
    assert "continuing from: fix the flaky parser test" in teleported.confirmation


def test_the_target_logs_the_teleport(teleported):
    rows = [json.loads(line) for line in (Path(teleported.box["ONE_CONV_LIVE_DIR"]) / "teleports.jsonl").read_text().splitlines()]
    assert [row["outcome"] for row in rows if row["kind"] == "spawn"] == ["accepted"]
