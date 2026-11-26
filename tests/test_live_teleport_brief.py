"""The provisioning brief: install sources, MCP config with secrets blanked, skills, logins and the rendered markdown."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from one_conv.live import brief

HOME = "/home/me"


def no_git(cwd, *args):
    return ""


@pytest.mark.parametrize(("path", "kind", "command"), [
    ("/opt/homebrew/Cellar/gh/2.63.0/bin/gh", "brew", "brew install gh"),
    ("/usr/local/Caskroom/docker/4.1/Docker.app/x", "brew", "brew install --cask docker"),
    ("/home/me/.local/share/uv/tools/ruff/bin/ruff", "uv", "uv tool install ruff"),
    ("/home/me/.local/pipx/venvs/black/bin/black", "pipx", "pipx install black"),
    ("/usr/lib/node_modules/wrangler/bin/wrangler.js", "npm", "npm install -g wrangler"),
    ("/usr/lib/node_modules/@openai/codex/bin/codex.js", "npm", "npm install -g @openai/codex"),
    ("/home/me/.cargo/bin/rg", "cargo", "cargo install rg"),
    ("/home/me/.local/bin/uv", "script", "curl -LsSf https://astral.sh/uv/install.sh | sh"),
    ("/home/me/.foundry/bin/forge", "script", "curl -L https://foundry.paradigm.xyz | bash && foundryup"),
    ("/opt/tools/odd/odd", "unknown", "find how to install `odd` (on the origin it is /opt/tools/odd/odd)"),
    ("/usr/bin/curl", "system", "install `curl` with the system's package manager"),
])
def test_install_source_reads_how_the_origin_installed_a_tool(path, kind, command):
    assert (lambda s: (s["kind"], s["command"]))(brief.install_source(Path(path).name, path, HOME, no_git)) == (kind, command)


@pytest.fixture
def checkout_tool(tmp_path):
    """`wt` on PATH is a symlink to bin/wt in a git checkout whose remote is known."""
    repo = tmp_path / "worktree-cli"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "wt").write_text("#!/bin/sh\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:me/worktree-cli.git"], check=True)
    link = tmp_path / "bin" / "wt"
    link.parent.mkdir()
    link.symlink_to(repo / "bin" / "wt")
    return brief.install_source("wt", str(link), str(tmp_path))


@pytest.mark.parametrize(("field", "value"), [
    ("kind", "git"),
    ("package", "git@github.com:me/worktree-cli.git"),
    ("launcher", "bin/wt"),
])
def test_a_tool_linked_from_a_checkout_names_its_remote_and_launcher(checkout_tool, field, value):
    assert checkout_tool[field] == value


@pytest.mark.parametrize(("source", "note"), [
    ({"kind": "brew", "package": "jq", "command": "brew install jq"}, "on Linux: `sudo apt-get install jq`"),
    ({"kind": "brew", "package": "fd", "command": "brew install fd"}, "on Linux: `sudo apt-get install fd-find`"),
    ({"kind": "brew", "package": "docker", "command": "brew install --cask docker"},
     "`docker` is a macOS app (Homebrew cask): there is no Linux install of it."),
    ({"kind": "uv", "package": "ruff", "command": "uv tool install ruff"}, ""),
])
def test_linux_note_offers_the_linux_equivalent_of_brew(source, note):
    assert brief.linux_note(source) == note


def test_redact_blanks_env_and_header_values():
    entry = {"command": "npx", "args": ["-y", "srv"], "env": {"API_KEY": "sk-123"},
             "headers": {"Authorization": "Bearer x"}}
    assert brief.redact(entry) == {"command": "npx", "args": ["-y", "srv"], "env": {"API_KEY": "<set me>"},
                                   "headers": {"Authorization": "<set me>"}}


@pytest.fixture
def configs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "claude").mkdir()
    (tmp_path / "codex").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"linear": {"type": "http", "url": "https://mcp.linear.app", "headers": {"Authorization": "x"}}}}))
    (tmp_path / "codex" / "config.toml").write_text('[mcp_servers.one-conv]\ncommand = "uv"\nenv = { TOKEN = "s" }\n')
    return tmp_path


def test_a_claude_mcp_entry_comes_from_its_config_redacted(configs):
    assert brief.mcp_entry("claude", "linear")[1]["headers"] == {"Authorization": "<set me>"}


def test_a_codex_mcp_entry_matches_the_tool_names_spelling(configs):
    assert brief.mcp_entry("codex", "one_conv") == ("one-conv", {"command": "uv", "env": {"TOKEN": "<set me>"}})


def test_claude_mcp_setup_is_an_add_json_command():
    assert brief.mcp_setup("claude", "linear", {"type": "http"}) == \
        "claude mcp add-json --scope user linear '{\"type\": \"http\"}'"


def test_codex_mcp_setup_is_a_config_table():
    assert brief.mcp_setup("codex", "one-conv", {"command": "uv", "env": {"TOKEN": "<set me>"}}).splitlines()[1:] == \
        ["[mcp_servers.one-conv]", 'command = "uv"', 'env = { "TOKEN" = "<set me>" }']


@pytest.mark.parametrize(("hint", "clis", "step"), [
    ("~/.config/gh/hosts.yml", [], "run `gh auth login`"),
    ("~/.wrangler/config/default.toml", [], "run `wrangler login`"),
    ("~/.aws/credentials", [], "run `aws configure (or aws sso login)`"),
    ("~/.config/vercel/auth.json", [], "run `vercel login`"),
    ("~/.config/wacli/session.db", ["wacli"],
     "probably `wacli login` (check `wacli --help`), else copy `~/.config/wacli/session.db` from the origin by hand"),
    ("~/.netrc", [], "copy `~/.netrc` from the origin by hand (scp); it may hold credentials, so never paste its "
                     "contents into a chat"),
])
def test_login_step_names_the_login_command_or_a_manual_copy(hint, clis, step):
    assert brief.login_step(hint, clis) == step


@pytest.mark.parametrize(("usage", "key", "text"), [
    ({"cli:wa": {"count": 12, "example": "wa send"}}, "cli:wa", "ran `wa send` (12 times in all)"),
    ({"cli:gh": {"count": 1, "example": "gh pr"}}, "cli:gh", "ran `gh pr`"),
    ({"mcp:linear": {"count": 3, "example": "list_issues"}}, "mcp:linear", "called its tools 3 times (e.g. `list_issues`)"),
    ({}, "cli:x", "used by the session"),
])
def test_why_says_how_the_session_used_the_item(usage, key, text):
    assert brief.why(usage, key) == text


@pytest.fixture
def skills(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("HOME", str(tmp_path))
    plain = tmp_path / "claude" / "skills" / "notes"
    plain.mkdir(parents=True)
    (plain / "SKILL.md").write_text("x")
    repo = tmp_path / "skills-repo"
    (repo / "tdd").mkdir(parents=True)
    (repo / "tdd" / "SKILL.md").write_text("x")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/me/skills"], check=True)
    (tmp_path / "claude" / "skills" / "tdd").symlink_to(repo / "tdd")
    return tmp_path


@pytest.mark.parametrize(("skill", "part"), [
    ("tdd", "comes from https://github.com/me/skills (path `tdd`)"),
    ("notes", "copy the folder `~/claude/skills/notes` from the origin (it is not in a git repository)"),
    ("codex:rescue", "install the `codex` Claude Code plugin"),
    ("nowhere", "copy the `nowhere` skill folder from the origin"),
])
def test_skill_source_says_where_the_skill_comes_from(skills, skill, part):
    assert part in brief.skill_source(skill)


@pytest.fixture
def rendered(configs):
    inventory = {"harness": "claude", "cwd": "/work", "clis": ["wa", "gh"],
                 "repo": {"remote": "git@github.com:o/app.git", "key": "github.com/o/app", "root": "/work"},
                 "usage": {"cli:wa": {"count": 12, "example": "wa send"}, "mcp:linear": {"count": 2, "example": "x"}}}
    rows = [{"item": "cli", "name": "wa", "status": "missing", "detail": ""},
            {"item": "cli", "name": "git", "status": "ok", "detail": ""},
            {"item": "mcp", "name": "linear", "status": "missing", "detail": ""},
            {"item": "hint", "name": "~/.config/gh/hosts.yml", "status": "missing", "detail": ""},
            {"item": "repo", "name": "github.com/o/app", "status": "missing", "detail": "no clone here yet"}]
    which = {"wa": "/opt/homebrew/Cellar/wacli/1.0/bin/wa"}.get
    return brief.render(inventory, rows, "box", "linux", "mac/claude:c1",
                        {"skipped": [{"path": ".env", "reason": "looks like a secret"}]}, which=which, home=HOME, origin_platform="darwin")


@pytest.mark.parametrize("part", [
    "## cli `wa` (missing)",
    "- Why: ran `wa send` (12 times in all).",
    "- Install: `brew install wacli`",
    "on Linux: `sudo apt-get install wacli` if the distribution packages it",
    "claude mcp add-json --scope user linear",
    '"Authorization": "<set me>"',
    "- Restore: run `gh auth login`",
    "git clone git@github.com:o/app.git ~/code/app",
    "- `.env` (looks like a secret)",
])
def test_the_brief_covers_each_gap(rendered, part):
    assert part in rendered


def test_the_brief_leaves_out_what_the_target_has(rendered):
    assert "`git`" not in rendered


def test_nothing_to_provision_renders_no_brief():
    assert brief.render({"harness": "claude"}, [{"item": "cli", "name": "git", "status": "ok", "detail": ""}], "box") == ""


def test_the_saved_brief_is_private(tmp_path):
    path = brief.save("# brief", "s1")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
