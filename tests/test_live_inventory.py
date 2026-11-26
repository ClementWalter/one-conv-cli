"""`live teleport`'s inventory: programs, MCP servers, skills, dotfile hints, session settings and git state."""

import subprocess

import pytest

from one_conv.live import inventory

HOME = "/home/me"


def bash(command: str) -> dict:
    return {"type": "tool_use", "name": "Bash", "input": {"command": command}}


@pytest.mark.parametrize(("command", "words"), [
    ("git status", ["git"]),
    ("cd repo && uv run pytest", ["uv"]),
    ("FOO=1 BAR=2 make test", ["make"]),
    ("gh pr view 12 | jq .title", ["gh", "jq"]),
    ("sudo apt-get install x; echo done", ["apt-get"]),
    ('python3 -c "import a; print(True)"', ["python3"]),
    ("echo $(git rev-parse HEAD)", ["git"]),
    ("./scripts/run.sh --fast", []),
    ("/usr/bin/env python3 x.py", []),
    ("ls 2>&1 | tail -3", ["ls", "tail"]),
    ("for f in *.py; do\n  ruff check $f\ndone", ["ruff"]),
    ("cd repo && if git diff --quiet; then make; fi", ["git", "make"]),
    ("cat <<'EOF' > notes.md\nClaude says; True\nEOF\nwc -l notes.md", ["cat", "wc"]),
    ("cat << EOF\nhello | world\nEOF", ["cat"]),
    ("npm test # then deploy | wrangler", ["npm"]),
    ("echo 'unbalanced", []),
])
def test_first_words_name_each_program_a_command_runs(command, words):
    assert inventory.first_words(command) == words


@pytest.mark.parametrize(("block", "commands"), [
    (bash("git log"), ["git log"]),
    ({"name": "shell", "input": {"command": ["bash", "-lc", "cargo build"]}}, ["cargo build"]),
    ({"name": "shell", "input": {"command": ["ls", "-la"]}}, ["ls -la"]),
    ({"name": "exec_command", "input": {"cmd": "rg foo"}}, ["rg foo"]),
    ({"name": "run_command", "input": {"CommandLine": "go test ./..."}}, ["go test ./..."]),
    ({"name": "exec", "input": 'text(await tools.exec_command({cmd:"git fetch origin main","max_output_tokens":2000}));'},
     ["git fetch origin main"]),
    ({"name": "Read", "input": {"file_path": "/x"}}, []),
])
def test_shell_commands_read_each_harness_tool(block, commands):
    assert inventory.shell_commands(block) == commands


@pytest.mark.parametrize(("tool", "server"), [
    ("mcp__one-conv__send_message", "one-conv"),
    ("mcp__claude_ai_Slack__slack_send_message", "claude_ai_Slack"),
    ("mcp__one_conv__set_name", "one_conv"),
    ("Bash", None),
    ("mcp__broken", None),
])
def test_mcp_server_is_the_middle_of_the_tool_name(tool, server):
    assert inventory.mcp_server(tool) == server


@pytest.mark.parametrize(("path", "hint"), [
    ("/home/me/.config/gh/hosts.yml", "~/.config/gh/hosts.yml"),
    ("~/.aws/credentials", "~/.aws/credentials"),
    ("/home/me/.netrc;", "~/.netrc"),
    ("/home/me/Documents/notes.md", None),
    ("/home/me/.claude/settings.json", None),
    ("/home/me/.claude.json", None),
    ("/home/me/.config/*.json", None),
    ("/home/me/code/app/.env", None),
    ("relative/.env", None),
    ("/etc/hosts", None),
])
def test_home_hint_keeps_only_dotfiles_outside_the_repo_and_agent_state(path, hint):
    assert inventory.home_hint(path, HOME, "/home/me/code/app") == hint


@pytest.mark.parametrize(("path", "skill"), [
    ("/home/me/.claude/skills/one-conv-cli/SKILL.md", "one-conv-cli"),
    ("/home/me/.codex/skills/thermo-review/SKILL.md", "thermo-review"),
    ("/home/me/.claude/skills/one-conv-cli/README.md", None),
])
def test_skill_of_reads_the_skill_folder_name(path, skill):
    assert inventory.skill_of(path) == skill


@pytest.fixture
def scanned():
    blocks = [
        bash("git status && uv run pytest"),
        {"name": "mcp__one-conv__send_message", "input": {}},
        {"name": "Skill", "input": {"skill": "codex:rescue"}},
        {"name": "Read", "input": {"file_path": "/home/me/.config/gh/hosts.yml"}},
        {"name": "Read", "input": {"file_path": "/home/me/.config/gone.yml"}},
        bash("cat /home/me/.claude/skills/tdd/SKILL.md"),
        {"name": "exec", "input": "await tools.mcp__linear__list_issues({});"},
        bash("git diff"),
    ]
    return inventory.scan(blocks, HOME, "/home/me/code/app", exists=lambda path: "gone" not in path)


@pytest.mark.parametrize(("kind", "expected"), [
    ("programs", ["git", "uv", "cat"]),
    ("mcp", ["one-conv", "linear"]),
    ("skills", ["codex:rescue", "tdd"]),
    ("hints", ["~/.config/gh/hosts.yml"]),
])
def test_scan_collects_each_kind_once_in_first_use_order(scanned, kind, expected):
    assert scanned[kind] == expected


def test_available_keeps_programs_found_on_path():
    assert inventory.available(["git", "nope"], lambda name: f"/usr/bin/{name}" if name == "git" else None) == ["git"]


def test_available_drops_a_name_a_case_insensitive_disk_resolves_to_another():
    assert inventory.available(["True"], lambda name: "/usr/bin/true") == []


@pytest.mark.parametrize(("text", "harness", "settings"), [
    ('{"model":"claude-opus-5-5","permissionMode":"bypassPermissions"}', "claude", ("claude-opus-5-5", "yolo")),
    ('{"permissionMode":"default"}\n{"permissionMode":"auto"}', "claude", ("", "auto")),
    ('{"permissionMode":"acceptEdits"}', "claude", ("", "ask")),
    ('{"model":"gpt-6","approval_policy":"never"}', "codex", ("gpt-6", "yolo")),
    ('{"approval_policy":"on-request","approvals_reviewer":"auto_review"}', "codex", ("", "auto")),
    ('{"approval_policy":"on-request"}', "codex", ("", "ask")),
    ('{"model":"m"}', "omp", ("m", "")),
])
def test_session_settings_read_the_last_model_and_mode(text, harness, settings):
    assert inventory.session_settings(text, harness) == settings


@pytest.mark.parametrize(("url", "key"), [
    ("git@github.com:Owner/Repo.git", "github.com/Owner/Repo"),
    ("https://github.com/Owner/Repo", "github.com/Owner/Repo"),
    ("https://user@GitHub.com/Owner/Repo.git/", "github.com/Owner/Repo"),
    ("ssh://git@gitlab.example.com:2222/team/app.git", "gitlab.example.com/2222/team/app"),
    ("/srv/git/app.git", "/srv/git/app"),
    ("file:///srv/git/app.git", "/srv/git/app"),
])
def test_remote_key_makes_ssh_and_https_clones_equal(url, key):
    assert inventory.remote_key(url) == key


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


@pytest.fixture
def checkout(tmp_path):
    """A clone of a bare remote with one pushed commit, one local commit, a changed file and an untracked one."""
    remote, clone = tmp_path / "remote.git", tmp_path / "clone"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", str(remote), str(clone))
    (clone / "a.txt").write_text("a")
    (clone / "pkg").mkdir()
    git(clone, "add", ".")
    git(clone, "commit", "-m", "one")
    git(clone, "push", "origin", "main")
    (clone / "b.txt").write_text("b")
    git(clone, "add", "b.txt")
    git(clone, "commit", "-m", "two")
    (clone / "a.txt").write_text("changed")
    (clone / "new.txt").write_text("new")
    (clone / "pkg" / "x").write_text("x")
    return inventory.repo(str(clone / "pkg"))


@pytest.mark.parametrize(("field", "value"), [
    ("branch", "main"),
    ("subdir", "pkg"),
    ("upstream", "origin/main"),
    ("changed", 1),
    ("untracked", 2),
    ("unpushed", 1),
])
def test_repo_reports_the_work_only_this_machine_has(checkout, field, value):
    assert getattr(checkout, field) == value


def test_repo_is_none_outside_git(tmp_path):
    assert inventory.repo(str(tmp_path)) is None
