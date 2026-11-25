"""Tell users of a git checkout or the Desktop extension that GitHub has a newer one-conv.

Nothing updates on its own: the check only reads GitHub, at most once a day, and caches the
answer next to the rest of the one-conv state. A checkout compares its HEAD with `main`; the
Desktop extension compares its manifest version with the latest release. A copy with neither
cannot be versioned and is never reported as outdated.
"""

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = "ClementWalter/one-conv-cli"
API = f"https://api.github.com/repos/{REPO}"
EXTENSION_URL = f"https://github.com/{REPO}/releases/latest/download/one-conv-cli.mcpb"
ROOT = Path(__file__).resolve().parents[1]
TTL = 24 * 3600
# The CLI repeats a notice at most this often; MCP sessions get it once each, at start.
SHOW_EVERY = 24 * 3600
DISABLE_ENV = "ONE_CONV_NO_UPDATE_CHECK"
log = logging.getLogger(__name__)


@dataclass
class Status:
    kind: str  # "git", "extension" or "copy"
    local: str
    latest: str = ""
    behind: int = 0
    subjects: list[str] = field(default_factory=list)
    checked_at: float = 0.0
    shown_at: float = 0.0

    @property
    def outdated(self) -> bool:
        return self.behind > 0


def _git(*args: str, root: Path = ROOT) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _manifest(root: Path) -> Path:
    # The extension unpacks the checkout at <extension>/cli/<repo>/, beside its manifest.
    return root.parent.parent / "manifest.json"


def installed(root: Path = ROOT) -> Status:
    """What is installed at `root`, without contacting GitHub."""
    if (root / ".git").exists():
        return Status("git", _git("rev-parse", "HEAD", root=root))
    manifest = _manifest(root)
    if manifest.is_file():
        try:
            return Status("extension", str(json.loads(manifest.read_text()).get("version", "")))
        except (OSError, ValueError):
            pass
    return Status("copy", "")


def _get(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "one-conv-update-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.lstrip("v").split("."))
    except ValueError:
        return ()


def fetch(status: Status, timeout: float = 3.0, get=_get) -> Status:
    """`status` with what GitHub has; unchanged (behind 0) when GitHub cannot say."""
    try:
        if status.kind == "git" and status.local:
            # base...head counts the commits on main that HEAD lacks; an unpushed HEAD is a 404.
            data = get(f"{API}/compare/{status.local}...main", timeout)
            commits = data.get("commits", [])
            status.behind = int(data.get("ahead_by", 0))
            status.latest = commits[-1]["sha"] if commits else status.local
            # Merge commits only repeat a PR number; the work's own subjects say what changed.
            status.subjects = [c["commit"]["message"].splitlines()[0] for c in reversed(commits)
                               if len(c.get("parents", [])) < 2][:5]
        elif status.kind == "extension" and status.local:
            status.latest = str(get(f"{API}/releases/latest", timeout).get("tag_name", "")).lstrip("v")
            status.behind = int(_version(status.latest) > _version(status.local) > ())
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
        log.debug("update check failed: %s", error)
    status.checked_at = time.time()
    return status


def _load(path: Path) -> Status | None:
    try:
        return Status(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def _save(path: Path, status: Status) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(status)))
    except OSError as error:
        log.debug("cannot save update check: %s", error)


def check(state_dir: Path, *, force: bool = False, root: Path = ROOT, get=_get, now=time.time) -> Status:
    """The cached status, refreshed from GitHub once a day or when the installed version changed."""
    path = state_dir / "update-check.json"
    current = installed(root)
    cached = _load(path)
    if (not force and cached and cached.kind == current.kind and cached.local == current.local
            and now() - cached.checked_at < TTL):
        return cached
    if current.kind == "copy":
        return current
    status = fetch(current, get=get)
    status.shown_at = cached.shown_at if cached else 0.0
    _save(path, status)
    return status


def how_to_update(status: Status, root: Path = ROOT) -> str:
    if status.kind == "git":
        return f"git -C {root} pull --ff-only (or: one-conv update --yes)"
    return f"download {EXTENSION_URL} and double-click it"


def notice(status: Status, root: Path = ROOT) -> str:
    """One paragraph for a person, empty when up to date."""
    if not status.outdated:
        return ""
    if status.kind == "git":
        what = f"{status.behind} new commit{'s' * (status.behind > 1)} on GitHub main"
        recent = "; ".join(status.subjects[:3])
        what += f" (latest: {recent})" if recent else ""
    else:
        what = f"version {status.latest} is out (installed: {status.local})"
    return (f"one-conv update available: {what}. Update: {how_to_update(status, root)}. "
            "Tell the user; do not update unasked.")


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "") not in ("", "0")


def cli_notice(state_dir: Path, *, get=_get, now=time.time) -> str:
    """The notice for a CLI run: at most once a day, so agents calling often are not flooded."""
    if disabled():
        return ""
    status = check(state_dir, get=get, now=now)
    text = notice(status)
    if not text or now() - status.shown_at < SHOW_EVERY:
        return ""
    status.shown_at = now()
    _save(state_dir / "update-check.json", status)
    return text


def session_notice(state_dir: Path, *, get=_get) -> str:
    """The notice for an MCP session start: every session that loads one-conv sees it."""
    return "" if disabled() else notice(check(state_dir, get=get))


def pull(root: Path = ROOT) -> str:
    """Fast-forward a clean checkout of main to GitHub's; ValueError names what blocks it."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", root=root)
    if branch != "main":
        raise ValueError(f"the checkout is on {branch or 'an unknown branch'}, not main: switch to main first")
    if _git("status", "--porcelain", "--untracked-files=no", root=root):
        raise ValueError("the checkout has uncommitted changes: commit or stash them first")
    result = subprocess.run(["git", "-C", str(root), "pull", "--ff-only", "origin", "main"],
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git pull failed")
    return result.stdout.strip()
