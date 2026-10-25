"""The folder a spawner starts sessions in: one-conv instructions on disk, and fresh code before each start.

Any machine with a running bridge can be the one the hub picks, and any of them can go away
(the Mac sleeps, a cloud box stops). So a session must find, wherever it starts, how to reach
the team and the rule that work only survives once pushed. The spawner writes that guide as
AGENTS.md (CLAUDE.md imports it) and pulls the code it runs from before each start.
"""

import logging
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# First line of a guide this module owns; a file without it is the user's and is never touched.
MARKER = "<!-- one-conv workspace guide: rewritten by the spawner; edit one_conv/live/workspace.py instead -->"
CLAUDE_MD = f"{MARKER}\n@AGENTS.md\n"
CHECKOUT = Path(__file__).resolve().parents[2]

GUIDE = """{marker}
# one-conv workspace on {host}

You were started by the one-conv live spawner on **{node}** ({host}) because the human posted in a
channel where no session was running. The human follows and answers from their phone, through
the channels. This machine is one of several that can be picked: the Mac when it is awake, a
cloud box otherwise. **It can disappear at any moment**, so nothing that is not pushed survives.

## Git: pull before, push after, always

- Repositories live in `~/Documents/<name>`. Missing one? `gh repo clone ClementWalter/<name> ~/Documents/<name>`.
- Before any work in a repository: `git fetch && git pull --rebase` (or start a worktree from `origin/main`).
- Never switch the branch of `~/Documents/one-conv-cli`: the bridge runs from it. Work in a worktree:
  `git -C ~/Documents/one-conv-cli worktree add ../one-conv-cli-<topic> -b <branch> origin/main`.
- Commit and push every change as soon as it is made (branch + PR + merge when the repository's
  rules ask for it). Never end a turn with unpushed commits or uncommitted work.
- Read the repository's own CLAUDE.md / AGENTS.md before changing it.

## Talking to the team (one-conv MCP tools)

- `join_topic` with `work=true`: join the channel you work in; it catches you up on its history.
  Only posts in your work channel wake you. Join other channels without `work` to watch them.
- `ack` with the `message_id` of a human post as soon as you read it (the phone shows ✓).
- `send_message` to `#topic` (every member), `@name`, or an address `NODE/harness:id`.
  Say in the channel what you take before starting, and leave work someone else took to them.
- `set_name` gives you a name others can reach as `@name`; `list_live_sessions` shows who runs where.
- Same from a shell: `one-conv live send '#topic' 'text' --yes`, `one-conv live ls`, `one-conv live --help`.

## Taking over: any agent can continue any session

This is the core of one-conv: any agent can read any agent's session from any machine and pick
up its work at any time. When your first prompt lists sessions that worked in the channel
before, read the most recent one with `read_conversation` first and continue where it stopped.
Every machine uploads its sessions to the hub's archive about every minute, so even one on a
machine that is now asleep or gone can be read.

## Reading and searching any session, on any machine

- `search_conversations` (namespace `local`) searches every machine's archived agent sessions;
  hits carry an `address`.
- `read_conversation` with an address `NODE/harness:id` reads that session from whichever machine
  ran it, through the hub's archive; `around` (a time or a message id) gives the turns around it.
- `find_conversations` finds sessions by title or project; `read_conversation` with a cloud id
  reads ChatGPT / Claude chats.
- Shell: `one-conv local search 'text'`, `one-conv local thread NODE/harness:id --no-mark-read`.
"""


def guide(node: str) -> str:
    return GUIDE.format(marker=MARKER, node=node or "this machine", host=socket.gethostname())


def _ours(path: Path) -> bool:
    try:
        return path.read_text().startswith(MARKER)
    except OSError:
        return False


def write_guide(cwd: Path, node: str) -> None:
    """Write AGENTS.md and CLAUDE.md in a workspace folder, leaving any file the user wrote alone."""
    for name, text in (("AGENTS.md", guide(node)), ("CLAUDE.md", CLAUDE_MD)):
        path = cwd / name
        if path.exists() and not _ours(path):
            continue
        if not path.exists() or path.read_text() != text:
            path.write_text(text)


def _git(repository: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repository), *args], capture_output=True, text=True, check=False, timeout=60)


def is_repository(folder: Path) -> bool:
    try:
        return _git(folder, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def pull(repository: Path) -> bool:
    """Fast-forward a clean checkout from its upstream; a dirty or diverged one is left as it is."""
    def git(*args):
        return _git(repository, *args)
    if not is_repository(repository):
        return False
    try:
        if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            logger.warning("not pulling %s: it has uncommitted changes", repository)
            return False
        result = git("pull", "--ff-only", "--quiet")
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("could not pull %s: %s", repository, error)
        return False
    if result.returncode:
        logger.warning("could not pull %s: %s", repository, result.stderr.strip())
    return result.returncode == 0


def prepare(cwd: Path, node: str) -> None:
    """Fresh one-conv code and the guide in place, so the session starts on what is pushed."""
    pull(CHECKOUT)
    if is_repository(cwd):
        pull(cwd)  # A repository keeps its own instructions; the spawn prompt still names the git rule.
    else:
        write_guide(cwd, node)
