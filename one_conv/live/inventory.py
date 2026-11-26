"""What a session needs to keep working on another machine, read from its own transcript.

`live teleport` takes this inventory before moving a session: the harness, model and permission
mode it runs with; its working folder's git repository (remote, branch, commit, work that exists
only here); the command-line tools its shell commands started; the MCP servers and skills it
used; and the dotfiles under the home folder it read, by name only, as hints that a credential
or a setting lives there. The heuristics are deliberately plain: a tool is the first word of
each shell command, an MCP server is the middle of an `mcp__server__tool` name, a skill is a
`Skill` call or a `skills/<name>/SKILL.md` it read.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Tool names under which harnesses run a shell command, and the input keys holding the command.
SHELL_TOOLS = frozenset({"Bash", "bash", "shell", "exec_command", "run_terminal_cmd", "run_terminal_command",
                         "run_command", "shell_command", "local_shell"})
COMMAND_KEYS = ("command", "cmd", "CommandLine", "commandLine")
# Tool names under which harnesses read a file, and the input keys holding its path.
READ_TOOLS = frozenset({"Read", "read", "read_file", "view", "view_file", "open_file"})
PATH_KEYS = ("file_path", "path", "target_file", "AbsolutePath", "filePath")
# Codex's code mode runs JavaScript calling `tools.<name>(...)`; a shell command is `exec_command({cmd: "..."})`.
CODE_TOOLS = frozenset({"exec"})
CODE_COMMAND = re.compile(r'exec_command\(\{\s*cmd:\s*("(?:[^"\\]|\\.)*")')
CODE_MCP = re.compile(r"tools\.(mcp__[A-Za-z0-9_-]+)")
# Words after which the next word is the program: wrappers and keywords that open a command.
LEADING_WORDS = frozenset({"sudo", "env", "time", "nohup", "noglob", "exec", "command", "builtin", "nice",
                           "if", "then", "do", "else", "elif", "while", "until", "!", "{"})
# The shell's own commands: the whole simple command they start runs no program.
SHELL_BUILTINS = frozenset({
    "cd", "echo", "export", "set", "unset", "source", ".", "true", "false", "test", "[", "[[", "exit", "for",
    "case", "esac", "printf", "read", "local", "return", "eval", "alias", "type", "wait", "pwd", "pushd",
    "popd", "shift", "trap", "ulimit", "umask", "let", "declare", "typeset", "function", "jobs", "fg", "bg",
    "history", "hash", "break", "continue", "select", "done", "fi", "in", "}", "kill",
})
# Characters that end one simple command and start the next.
PUNCTUATION = ";&|()\n`"
# A program run by name; a leading digit is a redirection like 2>&1.
PROGRAM = re.compile(r"[A-Za-z_][A-Za-z0-9_.+-]*")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SKILL_FILE = re.compile(r"skills/([A-Za-z0-9_.:-]+)/SKILL\.md$")
# Claude Code's permission modes, and Codex's approval settings, as `live spawn --mode`.
CLAUDE_MODES = {"bypassPermissions": "yolo", "auto": "auto"}
# Dot folders under the home folder that hold agent, editor or package-manager state, not a task's credentials.
HOME_STATE = frozenset({".claude", ".codex", ".cursor", ".omp", ".agy", ".gemini", ".agents", ".antigravity",
                        ".local", ".cache", ".npm", ".bun", ".cargo", ".rustup", ".pyenv", ".nvm", ".vscode",
                        ".Trash", ".DS_Store", ".zsh_history", ".bash_history", ".zcompdump", ".oh-my-zsh",
                        ".venv", ".uv", ".deno", ".gradle", ".m2"})


@dataclass
class Repo:
    """The git checkout a session works in, as this machine sees it."""

    root: str
    remote: str = ""
    key: str = ""
    branch: str = ""
    sha: str = ""
    subdir: str = ""
    upstream: str = ""
    # Tracked files changed (staged or not) and files git does not know, outside .gitignore.
    changed: int = 0
    untracked: int = 0
    # Commits on HEAD that no remote branch has.
    unpushed: int = 0


@dataclass
class Inventory:
    harness: str
    cwd: str
    model: str = ""
    mode: str = ""
    repo: Repo | None = None
    clis: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    # "cli:gh" -> {"count": 12, "example": "gh pr"}: why the session needs each item.
    usage: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


def shell_commands(block: dict) -> list[str]:
    """The shell commands one tool call ran: a shell tool's command, or each `exec_command` in code mode."""
    name, data = block.get("name"), block.get("input")
    if name in CODE_TOOLS and isinstance(data, str):
        found = []
        for literal in CODE_COMMAND.findall(data):
            try:
                found.append(json.loads(literal))
            except ValueError:
                continue
        return found
    if name not in SHELL_TOOLS or not isinstance(data, dict):
        return []
    command = next((data[key] for key in COMMAND_KEYS if key in data), None)
    if isinstance(command, list) and command:
        # Codex's older shell tool passes argv, often ["bash", "-lc", "<script>"].
        parts = [str(part) for part in command]
        return [parts[-1]] if len(parts) >= 3 and parts[-2] in ("-c", "-lc") else [shlex.join(parts)]
    return [command] if isinstance(command, str) else []


def tokens(command: str) -> list[str]:
    """Shell words and separators (; & | parentheses newline) with quotes respected and heredoc bodies dropped."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    # A newline is a separator, not blank space: it ends a command like `;` does.
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, "#"
    try:
        raw = list(lexer)
    except ValueError:
        # Unbalanced quotes: only the first line can still be read as words.
        raw = command.strip().splitlines()[0].split() if command.strip() else []
    kept, index = [], 0
    while index < len(raw):
        token = raw[index]
        kept.append(token)
        index += 1
        if not token.startswith("<<") or token.startswith("<<<"):
            continue
        terminator = token[2:].lstrip("-")
        if not terminator and index < len(raw):
            terminator = raw[index]
        # The body runs from the end of this line to a line holding only the terminator.
        while index < len(raw) and raw[index] != "\n":
            kept.append(raw[index])
            index += 1
        while index < len(raw) and not (raw[index] == terminator and raw[index - 1] == "\n"):
            index += 1
        index += 1
    return kept


def program_calls(command: str) -> list[tuple[str, str]]:
    """(program, `program subcommand`) for each simple command in `command`, skipping assignments and shell words."""
    calls, starting = [], True
    found = tokens(command)
    for index, token in enumerate(found):
        if all(char in PUNCTUATION for char in token):
            starting = True
            continue
        if not starting or ASSIGNMENT.match(token) or token in LEADING_WORDS:
            continue
        starting = False
        if token in SHELL_BUILTINS:
            continue
        # A path is a script in this checkout or this machine's layout, not a tool to install by name.
        if "/" in token or token.startswith(("$", "-", "~")) or not PROGRAM.fullmatch(token):
            continue
        following = found[index + 1] if index + 1 < len(found) else ""
        # A subcommand is a plain word, not a flag, path or separator.
        sub = following if PROGRAM.fullmatch(following) and not following.startswith("-") else ""
        calls.append((token, f"{token} {sub}" if sub else token))
    return calls


def first_words(command: str) -> list[str]:
    """The program each simple command in `command` starts."""
    return [program for program, _ in program_calls(command)]


def _count(usage: dict, key: str, example: str) -> None:
    """One more use of `key`; the example kept is the most frequent invocation."""
    row = usage.setdefault(key, {"count": 0, "examples": {}})
    row["count"] += 1
    row["examples"][example] = row["examples"].get(example, 0) + 1


def mcp_server(tool: str) -> str | None:
    """`server` of an `mcp__server__tool` name."""
    parts = tool.split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" and parts[1] else None


def home_hint(path: str, home: str, repo_root: str = "") -> str | None:
    """`~/.dir/...` for a file under a dot folder (or dotfile) of the home folder outside the repo, else None.

    Agent harness and package-manager state is skipped: it says nothing about the task's credentials.
    """
    path = path.rstrip(";,:)'\"")
    if any(char in path for char in "*?[$"):
        return None
    expanded = home + path[1:] if path.startswith("~/") else path
    if not os.path.isabs(expanded):
        return None
    expanded = os.path.normpath(expanded)
    if repo_root and (expanded == repo_root or expanded.startswith(repo_root.rstrip("/") + "/")):
        return None
    if not expanded.startswith(home.rstrip("/") + "/"):
        return None
    relative = expanded[len(home.rstrip("/")) + 1:]
    top = relative.split("/", 1)[0]
    if not top.startswith(".") or top in HOME_STATE or top.startswith(".claude.json"):
        return None
    return f"~/{relative}"


def skill_of(path: str) -> str | None:
    match = SKILL_FILE.search(path)
    return match[1] if match else None


def scan(blocks: list[dict], home: str, repo_root: str = "", exists=os.path.exists) -> dict[str, list[str]]:
    """Programs, MCP servers, skills and home dotfile hints in a transcript's tool calls, first use first.

    Programs are kept whatever this machine has; `available` filters them.
    """
    programs, servers, skills, hints, usage = {}, {}, {}, {}, {}
    for block in blocks:
        name = block.get("name")
        if not isinstance(name, str):
            continue
        data = block.get("input")
        tools = [name] if mcp_server(name) else []
        if name in CODE_TOOLS and isinstance(data, str):
            tools += CODE_MCP.findall(data)
        for tool in tools:
            server = mcp_server(tool)
            if server:
                servers[server] = None
                _count(usage, f"mcp:{server}", tool.split("__", 2)[2])
        if name == "Skill" and isinstance(data, dict) and isinstance(data.get("skill"), str):
            skills[data["skill"]] = None
            _count(usage, f"skill:{data['skill']}", "invoked")
        paths = []
        if name in READ_TOOLS and isinstance(data, dict):
            paths += [data[key] for key in PATH_KEYS if isinstance(data.get(key), str)]
        for command in shell_commands(block):
            for program, example in program_calls(command):
                programs[program] = None
                _count(usage, f"cli:{program}", example)
            paths += [token for token in tokens(command) if "/" in token]
        for path in paths:
            skill = skill_of(path)
            if skill:
                skills[skill] = None
                _count(usage, f"skill:{skill}", "read its SKILL.md")
            hint = home_hint(path, home, repo_root)
            if hint and not skill and exists(home.rstrip("/") + hint[1:]):
                hints[hint] = None
                _count(usage, f"hint:{hint}", "read")
    return {"programs": list(programs), "mcp": list(servers), "skills": list(skills), "hints": list(hints),
            "usage": {key: {"count": row["count"], "example": max(row["examples"], key=row["examples"].get)}
                      for key, row in usage.items()}}


def available(programs: list[str], which=shutil.which) -> list[str]:
    """The programs this machine resolves on PATH: one missing here was a typo, alias or function."""
    # The resolved name must match exactly: a case-insensitive disk resolves `True` to /usr/bin/true.
    return [program for program in programs if (found := which(program)) and Path(found).name == program]


def settings_from(path: str | None, harness: str) -> tuple[str, str]:
    """(model, mode) a session last ran with, from its transcript file; '' where it does not say."""
    if not path:
        return "", ""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return "", ""
    return session_settings(text, harness)


def session_settings(text: str, harness: str) -> tuple[str, str]:
    """(model, mode) from a transcript's text: the last model and permission setting it recorded."""
    models = re.findall(r'"model"\s*:\s*"([A-Za-z0-9._:/\[\]-]+)"', text)
    model = models[-1] if models else ""
    mode = ""
    if harness == "claude":
        modes = re.findall(r'"permissionMode"\s*:\s*"([A-Za-z]+)"', text)
        mode = CLAUDE_MODES.get(modes[-1], "ask") if modes else ""
    elif harness == "codex":
        policies = re.findall(r'"approval_policy"\s*:\s*"([a-z-]+)"', text)
        reviewers = re.findall(r'"approvals_reviewer"\s*:\s*"([a-z_]+)"', text)
        if policies:
            mode = "yolo" if policies[-1] == "never" else "auto" if reviewers and reviewers[-1] == "auto_review" else "ask"
    return model, mode


def _git(cwd: str, *args: str, timeout: float = 30) -> str | None:
    """stdout of a git command in `cwd`, or None when it fails."""
    try:
        result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout,
                                check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def remote_key(url: str) -> str:
    """A git remote URL reduced to host/path, so ssh and https clones of one repository compare equal."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    match = re.fullmatch(r"(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)[:/](.+)", url)
    if not match or url.startswith(("/", "file://")):
        return os.path.normpath(url.removeprefix("file://"))
    host, path = match[1].lower(), match[2].lstrip("/")
    return f"{host}/{path}"


def repo(cwd: str) -> Repo | None:
    """The checkout holding `cwd`, with what of its work exists only on this machine; None outside git."""
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None
    found = Repo(root=root)
    found.remote = _git(root, "remote", "get-url", "origin") or ""
    found.key = remote_key(found.remote) if found.remote else ""
    found.branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD") or ""
    found.sha = _git(root, "rev-parse", "HEAD") or ""
    relative = os.path.relpath(os.path.realpath(cwd), os.path.realpath(root))
    found.subdir = "" if relative == "." else relative
    found.upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or ""
    status = _git(root, "status", "--porcelain") or ""
    lines = [line for line in status.splitlines() if line]
    found.untracked = sum(1 for line in lines if line.startswith("??"))
    found.changed = len(lines) - found.untracked
    if found.sha:
        # Commits no remote-tracking branch holds: these exist on this machine only.
        count = _git(root, "rev-list", "--count", "HEAD", "--not", "--remotes")
        found.unpushed = int(count) if count and count.isdigit() else 0
    return found


def build(harness: str, cwd: str, blocks: list[dict], transcript_path: str | None = None,
          home: str | None = None, which=shutil.which) -> Inventory:
    """The inventory of a session of `harness` working in `cwd` whose tool calls are `blocks`."""
    home = home or str(Path.home())
    checkout = repo(cwd) if cwd and os.path.isdir(cwd) else None
    found = scan(blocks, home, checkout.root if checkout else "")
    model, mode = settings_from(transcript_path, harness)
    return Inventory(harness=harness, cwd=cwd, model=model, mode=mode, repo=checkout,
                     clis=available(found["programs"], which), mcp=found["mcp"], skills=found["skills"],
                     hints=found["hints"], usage=found["usage"])
