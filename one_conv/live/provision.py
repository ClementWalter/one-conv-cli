"""A teleport's target side: what this machine lacks for an incoming session, and starting it here.

The bridge (`live serve`) hands every relay request (a post with mode `request`) to `handle`.
A machine takes part only once its owner opted in (`live serve --accept-teleport`, saved in
serve.json); otherwise every request is refused with how to opt in. Requests come only through
the relay, from nodes holding a token on it, and the bridge's ingress log runs each at most once.

`probe` answers ok / missing / unknown for each item of the origin's inventory: the harness and
its version, every command-line tool, MCP server, skill and home dotfile, the repository (found
among the project roots by its normalized remote URL) and whether its commit can be fetched.
`spawn` checks the commit out without disturbing an existing checkout (a new worktree whenever
the checkout is on another branch, behind, or dirty), then starts the same harness in tmux the
way `live spawn` does, briefed to confirm to the origin and continue its task.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import names, router, spawn
from .inventory import remote_key
from .model import live_home

logger = logging.getLogger(__name__)

# serve.json keys: the opt-in, and where repositories and teleport worktrees live on this machine.
ACCEPT_KEY = "accept_teleport"
ROOTS_KEY = "project_roots"
WORKTREES_KEY = "worktree_root"
ROOTS_ENV = "ONE_CONV_PROJECT_ROOTS"
WORKTREES_ENV = "ONE_CONV_TELEPORT_WORKTREES"
DEFAULT_ROOTS = ("~/Documents", "~/code", "~/src", "~/projects", "~/dev", "~/git", "~/repos", "~/work",
                 "~/worktrees", "~/github")
DEFAULT_WORKTREES = "~/worktrees"
# How deep under a project root a checkout is looked for: root/repo and root/group/repo.
ROOT_DEPTH = 2
SKIPPED_DIRS = frozenset({"node_modules", "Library", "target", "build", "dist", "venv", ".venv"})
HARNESS_BINARY = {"claude": "claude", "codex": "codex", "cursor": "agent", "omp": "omp", "agy": "agy"}
# A request older than this when the bridge sees it is refused: its sender has stopped waiting.
REQUEST_TTL = 60.0
SPAWN_TIMEOUT = 150.0
GIT_TIMEOUT = 60.0


class ProvisionError(Exception):
    """A spawn that cannot go ahead; `screen` is the harness's last screen when it started but never registered."""

    def __init__(self, message: str, screen: str = ""):
        super().__init__(message)
        self.screen = screen


def audit(side: str, **row) -> None:
    """Append one teleport event to <live>/teleports.jsonl and the log: both machines keep a record."""
    entry = {"at": time.time(), "side": side, **row}
    logger.info("teleport %s: %s", side, json.dumps(row, ensure_ascii=False)[:500])
    try:
        path = live_home() / "teleports.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as error:
        logger.warning("teleport audit log not written: %s", error)


def _git(cwd: str, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout, check=False,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _out(cwd: str, *args: str) -> str | None:
    try:
        result = _git(cwd, *args)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def roots(settings: dict) -> list[Path]:
    """Folders searched for checkouts: ONE_CONV_PROJECT_ROOTS, else serve.json's project_roots, else common ones."""
    configured = os.environ.get(ROOTS_ENV)
    listed = configured.split(os.pathsep) if configured else settings.get(ROOTS_KEY) or DEFAULT_ROOTS
    return [Path(os.path.expanduser(str(root))) for root in listed if str(root).strip()]


def worktree_root(settings: dict) -> Path:
    return Path(os.path.expanduser(os.environ.get(WORKTREES_ENV) or settings.get(WORKTREES_KEY) or DEFAULT_WORKTREES))


def _checkouts(root: Path, depth: int = ROOT_DEPTH):
    """Folders holding `.git` at most `depth` levels under `root`, the root included."""
    if (root / ".git").exists():
        yield root
    if depth <= 0:
        return
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or entry.name in SKIPPED_DIRS:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from _checkouts(Path(entry.path), depth - 1)
        except OSError:
            continue


def find_repo(key: str, search: list[Path]) -> Path | None:
    """A checkout whose origin remote normalizes to `key`: a main checkout before a worktree, first root first."""
    worktrees = []
    for root in search:
        for folder in _checkouts(root):
            url = _out(str(folder), "remote", "get-url", "origin")
            if not url or remote_key(url) != key:
                continue
            if (folder / ".git").is_dir():
                return folder
            worktrees.append(folder)
    return worktrees[0] if worktrees else None


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _claude_config() -> dict:
    """~/.claude.json (or $CLAUDE_CONFIG_DIR/.claude.json): user and per-project MCP servers."""
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR") \
        else Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _installed_plugins() -> list[str]:
    try:
        data = json.loads((claude_home() / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return [key.partition("@")[0] for key in plugins] if isinstance(plugins, dict) else []


def _normal(name: str) -> str:
    # Codex writes `one-conv` as `one_conv` in tool names.
    return name.lower().replace("-", "_")


def mcp_status(harness: str, server: str, folder: str = "") -> str:
    """ok / missing / unknown: whether `harness` here has the MCP server a tool name called `server`."""
    if harness == "claude":
        if server.startswith("claude_ai_"):
            return "unknown"  # A claude.ai connector belongs to the account, not to a file on this machine.
        if server.startswith("plugin_"):
            return "ok" if any(_normal(server).startswith(f"plugin_{_normal(p)}_") for p in _installed_plugins()) \
                else "missing"
        config = _claude_config()
        names = set(config.get("mcpServers") or {})
        project = (config.get("projects") or {}).get(folder) or {}
        names |= set(project.get("mcpServers") or {})
        try:
            names |= set(json.loads((Path(folder) / ".mcp.json").read_text()).get("mcpServers") or {}) if folder else set()
        except (OSError, ValueError, AttributeError):
            pass
        return "ok" if _normal(server) in {_normal(n) for n in names} else "missing"
    if harness == "codex":
        import tomllib
        try:
            config = tomllib.loads((Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml").read_text())
        except (OSError, ValueError):
            return "missing"
        return "ok" if _normal(server) in {_normal(n) for n in config.get("mcp_servers") or {}} else "missing"
    return "unknown"


def skill_status(skill: str, folder: str = "") -> str:
    """ok / missing: a SKILL.md for `skill` (or `plugin:skill`) in this machine's or the project's skill folders."""
    plugin, _, name = skill.rpartition(":")
    homes = [claude_home() / "skills", Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills",
             Path.home() / ".agents" / "skills"]
    if folder:
        homes += [Path(folder) / ".claude" / "skills", Path(folder) / ".agents" / "skills"]
    if any((home / name / "SKILL.md").is_file() for home in homes):
        return "ok"
    pattern = f"*/{plugin or '*'}/*/skills/{name}/SKILL.md"
    return "ok" if next((claude_home() / "plugins" / "cache").glob(pattern), None) else "missing"


def harness_version(harness: str) -> tuple[str, str]:
    """(status, version) of the harness's command on this machine."""
    binary = shutil.which(HARNESS_BINARY.get(harness, harness))
    if not binary:
        return "missing", f"`{HARNESS_BINARY.get(harness, harness)}` is not on PATH"
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15, check=False)
        version = (result.stdout or result.stderr).strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        version = []
    return "ok", version[0][:80] if version else binary


def commit_status(folder: str, repo: dict, fetch_ref: str = "") -> tuple[str, str]:
    """ok when this checkout has the commit or the remote branch points at it; missing otherwise."""
    sha = repo.get("sha") or ""
    if not sha:
        return "unknown", "the origin has no commit"
    if _out(folder, "cat-file", "-e", f"{sha}^{{commit}}") is not None:
        return "ok", f"{sha[:12]} is here"
    ref = fetch_ref or repo.get("branch") or ""
    if not ref:
        return "missing", f"{sha[:12]} is not here and the origin is on a detached HEAD"
    listed = _out(folder, "ls-remote", "origin", f"refs/heads/{ref}")
    if listed is None:
        return "missing", f"cannot reach origin to look for {ref}"
    remote_sha = listed.split()[0] if listed else ""
    if remote_sha == sha:
        return "ok", f"fetchable from origin/{ref}"
    return "missing", f"{sha[:12]} is not on origin/{ref}: push it (or teleport with --push-wip)"


def row(item: str, name: str, status: str, detail: str = "") -> dict:
    return {"item": item, "name": name, "status": status, "detail": detail}


def locate(inventory: dict, settings: dict, cwd: str | None) -> tuple[str, list[dict]]:
    """The folder the session would work in here, and the rows saying how it was found."""
    repo = inventory.get("repo")
    if cwd:
        folder = os.path.expanduser(cwd)
        return (folder, [row("folder", folder, "ok", "given with --cwd")]) if os.path.isdir(folder) else \
            ("", [row("folder", folder, "missing", "given with --cwd, but it does not exist here")])
    if repo and repo.get("key"):
        found = find_repo(repo["key"], roots(settings))
        if found is None:
            searched = ", ".join(str(root) for root in roots(settings))
            return "", [row("repo", repo["key"], "missing",
                            f"no clone under {searched}: clone it, set project roots, or pass --cwd")]
        return str(found), [row("repo", repo["key"], "ok", str(found))]
    folder = inventory.get("cwd") or ""
    if folder and os.path.isdir(folder):
        return folder, [row("folder", folder, "ok", "same path as on the origin")]
    return "", [row("folder", folder or "?", "missing", "not a git checkout, and no such folder here: pass --cwd")]


def probe(inventory: dict, settings: dict, cwd: str | None = None, fetch_ref: str = "") -> dict:
    """Each item of the origin's inventory as ok / missing / unknown on this machine."""
    harness = str(inventory.get("harness") or "")
    status, version = harness_version(harness)
    rows = [row("harness", harness, status, version)]
    folder, located = locate(inventory, settings, cwd)
    rows += located
    repo = inventory.get("repo")
    if folder and repo and repo.get("sha") and _out(folder, "rev-parse", "--git-dir") is not None:
        rows.append(row("commit", f"{repo.get('branch') or 'HEAD'}@{repo['sha'][:12]}",
                        *commit_status(folder, repo, fetch_ref)))
    rows += [row("cli", name, "ok" if shutil.which(name) else "missing") for name in inventory.get("clis") or []]
    rows += [row("mcp", name, mcp_status(harness, name, folder)) for name in inventory.get("mcp") or []]
    rows += [row("skill", name, skill_status(name, folder)) for name in inventory.get("skills") or []]
    rows += [row("hint", name, "ok" if Path(os.path.expanduser(name)).exists() else "missing")
             for name in inventory.get("hints") or []]
    return {"rows": rows, "dir": folder}


def checkout(folder: str, repo: dict, fetch_ref: str, short: str, settings: dict) -> dict:
    """The working folder at the origin's commit: the checkout itself when it already is exactly there, else a new worktree.

    An existing checkout is never switched, reset or pulled. The worktree takes the origin's
    branch when no checkout here has it and it can only move forward to the commit; otherwise
    it gets its own `teleport/<short>` branch.
    """
    sha, branch = repo.get("sha") or "", repo.get("branch") or ""
    if fetch_ref:
        fetched = _git(folder, "fetch", "origin", f"+refs/heads/{fetch_ref}:refs/remotes/origin/{fetch_ref}")
        if fetched.returncode:
            logger.info("teleport: fetching %s in %s failed: %s", fetch_ref, folder, fetched.stderr.strip())
    if _out(folder, "cat-file", "-e", f"{sha}^{{commit}}") is None:
        raise ProvisionError(f"commit {sha[:12]} is not in {folder} and could not be fetched from origin "
                             f"{fetch_ref or branch}: push it first (or teleport with --push-wip)")
    current = _out(folder, "symbolic-ref", "--short", "-q", "HEAD") or ""
    clean = _out(folder, "status", "--porcelain") == ""
    if branch and current == branch and clean and _out(folder, "rev-parse", "HEAD") == sha:
        return {"dir": folder, "branch": branch, "sha": sha, "checkout": "the existing checkout"}
    name = Path(repo.get("key") or folder).name or "repo"
    path = worktree_root(settings) / name / f"teleport-{short}"
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = _out(folder, "worktree", "list", "--porcelain") or ""
    taken = {line.split(" ", 1)[1] for line in listed.splitlines() if line.startswith("branch ")}
    local = _out(folder, "rev-parse", "--verify", "-q", f"refs/heads/{branch}") if branch else None
    forward = local is None or _git(folder, "merge-base", "--is-ancestor", local, sha).returncode == 0
    if branch and f"refs/heads/{branch}" not in taken and forward:
        target, args = branch, ["worktree", "add", "-B", branch, str(path), sha]
    else:
        target, args = f"teleport/{short}", ["worktree", "add", "-b", f"teleport/{short}", str(path), sha]
    added = _git(folder, *args)
    if added.returncode:
        raise ProvisionError(f"git {' '.join(args[:2])} failed in {folder}: {added.stderr.strip()}")
    if target == branch and _out(folder, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"):
        _git(str(path), "branch", f"--set-upstream-to=origin/{branch}")
    return {"dir": str(path), "branch": target, "sha": sha, "checkout": f"a new worktree from {folder}"}


def brief(request: dict, node: str, place: dict) -> str:
    """The new session's first prompt: who it continues, the confirmation to send first, then the origin's seed."""
    origin, summary = request["origin"], request.get("summary") or "its last request"
    where = f"{place['branch']}@{place['sha'][:12]}" if place.get("sha") else "no git commit"
    worked_on = ((request.get("inventory") or {}).get("repo") or {}).get("branch") or ""
    wip = request.get("wip_branch")
    lines = [
        f"You are the continuation of the agent session {origin}, teleported to this machine ({node}) with "
        "`one-conv live teleport`. That session stops working once you confirm: from now on you own its task.",
        f"You work in {place['dir']} ({place['checkout']}), on {where}.",
    ]
    if wip:
        lines.append(f"Its commits not yet on the remote travelled on the branch origin/{wip}; once your branch is "
                     f"pushed, delete that branch (`git push origin --delete {wip}`).")
    if place.get("branch") and worked_on and place["branch"] != worked_on:
        lines.append(f"The origin worked on {worked_on}, which another checkout here holds or has moved past: "
                     f"push your commits to it with `git push origin HEAD:{worked_on}`.")
    lines += [
        "",
        f"FIRST, before anything else, send {origin} exactly one message with the one-conv send_message tool, "
        f'to="{origin}" and expect_reply=false, with this text (your address is what `one-conv live whoami` prints):',
        f"teleported: now {node}/<your address> in {place['dir']} at {where}; continuing from: {summary}",
        "",
        f"Then continue the last task of {origin} from where it stopped. Its conversation follows.",
        "",
        request.get("seed") or "",
    ]
    return "\n".join(lines)


def _harness_args(harness: str, model: str) -> list[str]:
    """The model flag: claude and codex start on the origin's model; the others use their own default."""
    if not model:
        return []
    return {"claude": ["--model", model], "codex": ["--model", model]}.get(harness, [])


def start_session(request: dict, node: str, settings: dict, timeout: float = SPAWN_TIMEOUT) -> dict:
    """Check the code out and start the harness in tmux, briefed; returns the new session's address and place."""
    inventory = request.get("inventory") or {}
    harness = inventory.get("harness")
    if harness not in spawn.SPAWNABLE:
        raise ProvisionError(f"cannot start {harness!r} sessions")
    if not shutil.which(HARNESS_BINARY[harness]):
        raise ProvisionError(f"`{HARNESS_BINARY[harness]}` is not installed on {node}")
    folder, located = locate(inventory, settings, request.get("cwd"))
    if not folder:
        raise ProvisionError("; ".join(f"{r['name']}: {r['detail']}" for r in located))
    repo = inventory.get("repo")
    short = str(request.get("short") or "x")
    if repo and repo.get("sha") and _out(folder, "rev-parse", "--git-dir") is not None:
        place = checkout(folder, repo, request.get("fetch") or repo.get("branch") or "", short, settings)
        if repo.get("subdir") and (Path(place["dir"]) / repo["subdir"]).is_dir():
            place["dir"] = str(Path(place["dir"]) / repo["subdir"])
    else:
        place = {"dir": folder, "branch": "", "sha": "", "checkout": "the given folder"}
    mode = request.get("mode") or "ask"
    try:
        command = spawn.launch(harness, brief(request, node, place), place["dir"],
                               _harness_args(harness, inventory.get("model") or ""), mode)
    except ValueError as error:
        raise ProvisionError(str(error)) from None
    adapter = router.ADAPTERS[harness]
    before = {session.id for session in adapter.list_sessions()}
    tmux = spawn.tmux_name(harness, place["dir"], spawn.tmux_taken)
    try:
        spawn.start(tmux, place["dir"], command)
    except (RuntimeError, OSError) as error:
        raise ProvisionError(str(error)) from None
    entry = spawn.Spawned(tmux, harness, place["dir"], "", request["origin"], time.time())
    spawn.record(entry)
    session = spawn.wait_for(adapter.list_sessions, before, place["dir"], timeout, lambda: spawn.tmux_taken(tmux))
    if session is None:
        state = "is still running" if spawn.tmux_taken(tmux) else "exited"
        raise ProvisionError(f"{harness} {state} on {node} but did not register within {timeout:g}s "
                             f"(attach there with: tmux attach -t {tmux})", spawn.screen(tmux))
    entry.address = session.address
    spawn.record(entry)
    result = {"address": f"{node}/{session.address}", "local": session.address, "tmux": tmux, **place}
    if request.get("name"):
        try:
            result["name"] = "@" + names.set_name(request["name"], session.address, lambda address: any(
                s.address == address for s in router.list_all(address.partition(":")[0])), replace=True)
        except names.NameRefused as error:
            result["name_error"] = str(error)
    return result


def handle(item: dict, node: str, settings: dict, now=time.time) -> tuple[str, dict]:
    """(outcome, result) for one relay request: `accepted` with the answer, or `refused` with `error`."""
    sender = str(item.get("from") or "")
    try:
        request = json.loads(item.get("text") or "")
    except ValueError:
        request = None
    if not isinstance(request, dict) or request.get("kind") not in ("probe", "spawn"):
        return "refused", {"error": "this bridge answers only teleport probe and spawn requests"}
    kind = request["kind"]
    if settings.get(ACCEPT_KEY) is not True:
        audit("target", kind=kind, sender=sender, outcome="refused", reason="not accepting teleports")
        return "refused", {"error": f"{node} does not accept teleports; on {node}, run "
                                    "`one-conv live serve --accept-teleport` to allow them"}
    created = item.get("created")
    ttl = request.get("ttl") if isinstance(request.get("ttl"), (int, float)) else REQUEST_TTL
    if isinstance(created, (int, float)) and now() - created > ttl:
        audit("target", kind=kind, sender=sender, outcome="refused", reason="expired")
        return "refused", {"error": f"the {kind} request reached {node} after {ttl:g}s; its sender stopped waiting"}
    if kind == "probe":
        answer = {"node": node, **probe(request.get("inventory") or {}, settings, request.get("cwd"),
                                        request.get("fetch") or "")}
        audit("target", kind=kind, sender=sender, outcome="accepted",
              missing=[r["name"] for r in answer["rows"] if r["status"] == "missing"])
        return "accepted", answer
    try:
        answer = {"node": node, **start_session(request, node, settings,
                                                float(request.get("timeout") or SPAWN_TIMEOUT))}
    except ProvisionError as error:
        audit("target", kind=kind, sender=sender, origin=request.get("origin"), outcome="refused", reason=str(error)[:500])
        return "refused", {"error": str(error), **({"screen": error.screen[-4000:]} if error.screen else {})}
    except (OSError, subprocess.SubprocessError) as error:
        audit("target", kind=kind, sender=sender, origin=request.get("origin"), outcome="refused", reason=str(error)[:500])
        return "refused", {"error": f"{type(error).__name__}: {error}"[:1000]}
    audit("target", kind=kind, sender=sender, origin=request.get("origin"), outcome="accepted",
          address=answer["address"], dir=answer["dir"])
    return "accepted", answer
