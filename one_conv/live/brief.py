"""A teleport's provisioning brief: what the target lacks, why the session needs it, and how to get it.

Gaps never refuse a teleport. The origin turns the target's ok / missing / unknown rows into a
markdown brief, saved under <live dir>/teleport-briefs/ and shipped to the target, where it is
step 0 of the continuation's first prompt. For each missing item it says what it is and why the
session needs it (how often it was used), how this machine installed it (Homebrew, uv tool, pipx,
npm -g, cargo, go install, a git checkout's launcher), the origin's MCP config entry with secret
values replaced by `<set me>`, a skill's folder and git remote, and how to log in where a dotfile
held credentials. Secret values are never read into it: only names and config keys.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import provision

SET_ME = "<set me>"
# Login commands for the dotfiles that hold a CLI's credentials.
LOGINS = (("~/.config/gh", "gh auth login"), ("~/.wrangler", "wrangler login"),
          ("~/.config/.wrangler", "wrangler login"), ("~/.aws", "aws configure (or aws sso login)"),
          ("~/.config/gcloud", "gcloud auth login"), ("~/.npmrc", "npm login"), ("~/.docker", "docker login"),
          ("~/.azure", "az login"), ("~/.fly", "fly auth login"), ("~/.config/vercel", "vercel login"),
          ("~/.netlify", "netlify login"), ("~/.config/glab-cli", "glab auth login"), ("~/.heroku", "heroku login"))
# CLIs whose `login` subcommand stores their credentials.
LOGIN_CLIS = {"gh": "gh auth login", "wrangler": "wrangler login", "vercel": "vercel login", "fly": "fly auth login",
              "flyctl": "flyctl auth login", "netlify": "netlify login", "heroku": "heroku login",
              "gcloud": "gcloud auth login", "az": "az login", "docker": "docker login", "npm": "npm login",
              "glab": "glab auth login", "railway": "railway login", "supabase": "supabase login"}
# Homebrew formulas whose Debian/Ubuntu package has another name.
APT_NAMES = {"ripgrep": "ripgrep", "fd": "fd-find", "node": "nodejs", "sqlite": "sqlite3", "gnu-sed": "sed",
             "coreutils": "coreutils", "jq": "jq", "tmux": "tmux", "git": "git", "wget": "wget", "make": "make",
             "cmake": "cmake", "htop": "htop", "tree": "tree", "pandoc": "pandoc", "ffmpeg": "ffmpeg",
             "postgresql": "postgresql-client", "redis": "redis-tools", "gh": "gh (after adding GitHub's apt repository)"}
# Tools usually installed by their own script rather than a package manager.
INSTALLERS = {"uv": "curl -LsSf https://astral.sh/uv/install.sh | sh", "uvx": "curl -LsSf https://astral.sh/uv/install.sh | sh",
              "forge": "curl -L https://foundry.paradigm.xyz | bash && foundryup",
              "cast": "curl -L https://foundry.paradigm.xyz | bash && foundryup",
              "anvil": "curl -L https://foundry.paradigm.xyz | bash && foundryup",
              "bun": "curl -fsSL https://bun.sh/install | bash", "deno": "curl -fsSL https://deno.land/install.sh | sh",
              "rustup": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
              "cargo": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"}
# Harnesses a person (or the bridge) can install without answering a prompt.
HARNESS_INSTALL = {"claude": "npm install -g @anthropic-ai/claude-code   # or: curl -fsSL https://claude.ai/install.sh | bash",
                   "codex": "npm install -g @openai/codex",
                   "cursor": "curl https://cursor.com/install -fsS | bash"}


def _git_out(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=30, check=False,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _hops(path: str) -> list[str]:
    """`path` and each file its symlinks lead to, in order."""
    hops, current = [path], path
    for _ in range(16):
        if not os.path.islink(current):
            break
        current = os.path.normpath(os.path.join(os.path.dirname(current), os.readlink(current)))
        hops.append(current)
    return hops


def install_source(program: str, path: str, home: str, git=_git_out) -> dict:
    """How `program`, found at `path` on this machine, was installed: {kind, package, command, note}."""
    hops = _hops(path)
    real = os.path.realpath(path)
    home = home.rstrip("/")
    for candidate in [*hops, real]:
        cellar = re.search(r"/Cellar/([^/]+)/", candidate)
        if cellar:
            return {"kind": "brew", "package": cellar[1], "command": f"brew install {cellar[1]}"}
        cask = re.search(r"/Caskroom/([^/]+)/", candidate)
        if cask:
            return {"kind": "brew", "package": cask[1], "command": f"brew install --cask {cask[1]}"}
        uv_tool = re.search(r"/uv/tools/([^/]+)/", candidate)
        if uv_tool:
            return {"kind": "uv", "package": uv_tool[1], "command": f"uv tool install {uv_tool[1]}"}
        pipx = re.search(r"/pipx/venvs/([^/]+)/", candidate)
        if pipx:
            return {"kind": "pipx", "package": pipx[1], "command": f"pipx install {pipx[1]}"}
        npm = re.search(r"/node_modules/((?:@[^/]+/)?[^/]+)/", candidate)
        if npm:
            return {"kind": "npm", "package": npm[1], "command": f"npm install -g {npm[1]}"}
    if hops[0].startswith(f"{home}/.cargo/bin/"):
        return {"kind": "cargo", "package": program, "command": f"cargo install {program}",
                "note": "the crate name may differ from the binary's; `cargo install --list` on the origin shows it"}
    if "/go/bin/" in hops[0]:
        module = _go_module(hops[0])
        return {"kind": "go", "package": module or program,
                "command": f"go install {module}@latest" if module else f"go install <module of {program}>@latest"}
    for hop in hops[1:] or hops:
        top = git(os.path.dirname(hop), "rev-parse", "--show-toplevel")
        if top:
            remote = git(top, "remote", "get-url", "origin")
            launcher = os.path.relpath(hop, top)
            name = Path(top).name
            clone = f"git clone {remote} ~/code/{name}" if remote else f"copy {top} from the origin (no remote)"
            return {"kind": "git", "package": remote or top, "launcher": launcher,
                    "command": f"{clone} && ln -s ~/code/{name}/{launcher} ~/.local/bin/{program}"}
    if program in INSTALLERS:
        return {"kind": "script", "package": program, "command": INSTALLERS[program]}
    if real.startswith(("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/", "/System/")):
        return {"kind": "system", "package": program, "command": f"install `{program}` with the system's package manager"}
    return {"kind": "unknown", "package": program, "command": f"find how to install `{program}` (on the origin it is {path})"}


def _go_module(binary: str) -> str:
    try:
        out = subprocess.run(["go", "version", "-m", binary], capture_output=True, text=True, timeout=15, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"^\s*path\s+(\S+)", out, re.M)
    return match[1] if match else ""


def linux_note(source: dict) -> str:
    """The Linux way when the origin installed it with Homebrew; '' when the command works there too."""
    if source.get("kind") != "brew":
        return ""
    formula = source["package"]
    if source["command"].startswith("brew install --cask"):
        return f"`{formula}` is a macOS app (Homebrew cask): there is no Linux install of it."
    apt = APT_NAMES.get(formula)
    if apt:
        return f"on Linux: `sudo apt-get install {apt}`"
    return (f"on Linux: `sudo apt-get install {formula}` if the distribution packages it, else Homebrew on Linux "
            f"(`brew install {formula}`)")


def redact(entry):
    """An MCP config entry with every env value and header value replaced by `<set me>`."""
    if not isinstance(entry, dict):
        return entry
    out = {}
    for key, value in entry.items():
        if key in ("env", "headers", "http_headers", "env_http_headers") and isinstance(value, dict):
            out[key] = {name: SET_ME for name in value}
        elif key in ("bearer_token", "token", "api_key", "apiKey"):
            out[key] = SET_ME
        else:
            out[key] = redact(value) if isinstance(value, dict) else value
    return out


def _normal(name: str) -> str:
    return name.lower().replace("-", "_")


def mcp_entry(harness: str, server: str, cwd: str = "") -> tuple[str, dict | None]:
    """(name as configured, redacted entry) of `server` in this machine's `harness` config, or (server, None)."""
    entries: dict = {}
    if harness == "claude":
        config = provision._claude_config()
        entries.update(config.get("mcpServers") or {})
        entries.update(((config.get("projects") or {}).get(cwd) or {}).get("mcpServers") or {})
    elif harness == "codex":
        import tomllib
        try:
            config = tomllib.loads((Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml").read_text())
        except (OSError, ValueError):
            config = {}
        entries.update(config.get("mcp_servers") or {})
    for name, entry in entries.items():
        if _normal(name) == _normal(server):
            return name, redact(entry)
    return server, None


def _toml_value(value) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value))


def mcp_setup(harness: str, name: str, entry: dict) -> str:
    """The command or config text that adds `entry` to `harness` on the target."""
    if harness == "claude":
        return f"claude mcp add-json --scope user {name} '{json.dumps(entry)}'"
    lines = [f"[mcp_servers.{name}]"] + [f"{key} = {_toml_value(value)}" for key, value in entry.items()]
    return "append to ~/.codex/config.toml:\n" + "\n".join(lines)


def skill_dir(skill: str, cwd: str = "") -> Path | None:
    """Where `skill` (or `plugin:skill`) lives on this machine."""
    plugin, _, name = skill.rpartition(":")
    homes = [provision.claude_home() / "skills", Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills",
             Path.home() / ".agents" / "skills"]
    if cwd:
        homes += [Path(cwd) / ".claude" / "skills", Path(cwd) / ".agents" / "skills"]
    for home in homes:
        if (home / name / "SKILL.md").is_file():
            return home / name
    found = next((provision.claude_home() / "plugins" / "cache").glob(f"*/{plugin or '*'}/*/skills/{name}/SKILL.md"), None)
    return found.parent if found else None


def skill_source(skill: str, cwd: str = "", git=_git_out) -> str:
    """How to get the skill's folder onto the target."""
    plugin, _, name = skill.rpartition(":")
    if plugin:
        return f"install the `{plugin}` Claude Code plugin (`/plugin install {plugin}`), which ships the `{name}` skill"
    folder = skill_dir(skill, cwd)
    if folder is None:
        return f"copy the `{name}` skill folder from the origin (not found in its skill folders)"
    real = os.path.realpath(folder)
    top = git(real, "rev-parse", "--show-toplevel")
    remote = git(top, "remote", "get-url", "origin") if top else ""
    home = str(Path.home())
    shown = str(folder).replace(home, "~", 1)
    if remote:
        return (f"on the origin `{shown}` comes from {remote} (path `{os.path.relpath(real, top)}`): clone it and link "
                f"that folder to `{shown}`")
    return f"copy the folder `{shown}` from the origin (it is not in a git repository)"


def login_step(hint: str, clis: list[str]) -> str:
    """How to restore the credentials a dotfile held, without copying its values through any chat."""
    for prefix, command in LOGINS:
        if hint == prefix or hint.startswith(prefix + "/"):
            return f"run `{command}`"
    parts = hint.split("/")
    tool = parts[2].lstrip(".") if len(parts) > 2 and parts[1] == ".config" else parts[1].lstrip(".")
    if tool in LOGIN_CLIS:
        return f"run `{LOGIN_CLIS[tool]}`"
    if tool in clis:
        return f"probably `{tool} login` (check `{tool} --help`), else copy `{hint}` from the origin by hand"
    return f"copy `{hint}` from the origin by hand (scp); it may hold credentials, so never paste its contents into a chat"


def why(usage: dict, key: str) -> str:
    row = usage.get(key)
    if not row:
        return "used by the session"
    count = row["count"]
    times = "once" if count == 1 else f"{count} times"
    kind = key.split(":", 1)[0]
    if kind == "cli":
        return f"ran `{row['example']}`" + (f" ({times} in all)" if count > 1 else "")
    if kind == "mcp":
        return f"called its tools {times} (e.g. `{row['example']}`)"
    if kind == "skill":
        return f"{row['example']} {times}"
    return f"read {times}"


def gaps(rows: list[dict]) -> list[dict]:
    """The rows the brief covers: what is missing, and MCP servers nobody can check from a file."""
    return [row for row in rows if row["status"] == "missing" or (row["item"] == "mcp" and row["status"] == "unknown")]


def render(inventory: dict, rows: list[dict], node: str, target_platform: str = "", origin: str = "",
           snapshot: dict | None = None, which=None, home: str | None = None,
           origin_platform: str = sys.platform) -> str:
    """The provisioning brief as markdown; empty when the target lacks nothing and nothing was left behind."""
    import shutil
    which = which or shutil.which
    home = home or str(Path.home())
    usage, clis = inventory.get("usage") or {}, inventory.get("clis") or []
    harness, cwd = inventory.get("harness") or "", inventory.get("cwd") or ""
    linux = target_platform.startswith("linux") and origin_platform == "darwin"
    skipped = (snapshot or {}).get("skipped") or []
    missing = gaps(rows)
    if not missing and not skipped:
        return ""
    lines = [f"# Provisioning {node} for {origin or 'a teleported session'}", "",
             f"The check found what {node} lacks for this session. Install and configure what you can "
             "non-interactively; anything that needs a person (logins, 2FA, secrets) goes in one note to the user.", ""]
    for row in missing:
        item, name = row["item"], row["name"]
        lines.append(f"## {item} `{name}` ({row['status']})")
        if item == "harness":
            lines.append(f"- Why: the session runs in {name}.")
            lines.append(f"- Install: `{HARNESS_INSTALL.get(name) or f'install {name} as on the origin'}`")
        elif item == "tmux":
            lines += ["- Why: the continuation runs in a detached tmux session.",
                      "- Install: `brew install tmux` (macOS) or `sudo apt-get install tmux` (Linux)"]
        elif item == "cli":
            lines.append(f"- Why: {why(usage, f'cli:{name}')}.")
            found = which(name)
            source = install_source(name, found, home) if found else {"kind": "unknown", "command": f"install `{name}`"}
            # Prose for an unknown origin, a command otherwise.
            how = source["command"] if source.get("kind") in ("unknown", "system") else f"`{source['command']}`"
            lines.append(f"- Install: {how}" + (f" ({source['note']})" if source.get("note") else ""))
            if source.get("kind") == "git":
                lines.append(f"- On the origin it is the launcher `{source['launcher']}` of the checkout {source['package']}.")
            note = linux_note(source) if linux else ""
            if note:
                lines.append(f"- Platform: {note}")
            if name in LOGIN_CLIS:
                lines.append(f"- Then log in: `{LOGIN_CLIS[name]}` (interactive: for the user).")
        elif item == "mcp":
            lines.append(f"- Why: {why(usage, f'mcp:{name}')}.")
            if name.startswith("claude_ai_"):
                lines.append("- A claude.ai connector: it comes with the Claude account; check it is enabled there.")
            else:
                configured, entry = mcp_entry(harness, name, cwd)
                if entry is None:
                    lines.append(f"- Not in the origin's {harness} config files; add the same server by hand.")
                else:
                    lines += ["- Configure (values shown as `<set me>` are secrets: ask the user for them):", "```",
                              mcp_setup(harness, configured, entry), "```"]
        elif item == "skill":
            lines.append(f"- Why: {why(usage, f'skill:{name}')}.")
            lines.append(f"- Get it: {skill_source(name, cwd)}.")
        elif item == "hint":
            lines.append(f"- Why: the session {why(usage, f'hint:{name}')} it; it likely holds a login or setting.")
            lines.append(f"- Restore: {login_step(name, clis)} (for the user: never copy or print the values).")
        elif item in ("repo", "folder"):
            repo = inventory.get("repo") or {}
            if repo.get("remote"):
                lines.append(f"- Clone: `git clone {repo['remote']} ~/code/{Path(repo['key'] or repo['root']).name}`")
            lines.append(f"- {row['detail']}")
        elif item == "commit":
            lines.append(f"- {row['detail']}")
        lines.append("")
    if skipped:
        lines += ["## Files left behind", "",
                  "The snapshot of uncommitted work left these out; copy them by hand if the task needs them:", ""]
        lines += [f"- `{entry['path']}` ({entry['reason']})" for entry in skipped]
        lines.append("")
    return "\n".join(lines)


def save(text: str, short: str) -> Path:
    """Write the brief under <live dir>/teleport-briefs/<short>.md, readable only by this user."""
    from .model import live_home
    path = live_home() / "teleport-briefs" / f"{short}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write(text)
    return path
