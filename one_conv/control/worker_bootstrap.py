"""Box-side worker helper: selected clones, portable skill links, resident packing health.

Copied onto a sandbox with `box scp` and given a JSON payload path. The payload is unlinked
after read. Stdlib only so the file can run before one-conv is on PATH. Tokens never appear
in logs or health JSON.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MODEL = re.compile(r"[A-Za-z0-9._:+-]{1,64}")
HARNESSES = ("claude", "codex", "omp")
SECRET_KEYS = ("service_account", "token", "hub_token", "GH_TOKEN", "GITHUB_TOKEN",
               "OP_SERVICE_ACCOUNT_TOKEN", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
               "CLAUDE_CODE_OAUTH_TOKEN")
PROFILE_ENV = {
    "claude_subscription": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "codex_native": ("CODEX_API_KEY",),
}
FIXED_HELPER_NAME = "one-conv-worker-bootstrap.py"


class BootstrapError(ValueError):
    """A payload or health document that cannot be used without guessing."""


def repositories(values) -> list[str]:
    """Restrict clone inputs to GitHub names and distinct destination folders."""
    if not isinstance(values, (list, tuple)) or not values:
        raise BootstrapError("repositories must use OWNER/REPO names without .git")
    result = list(dict.fromkeys(values))
    if any(not isinstance(value, str) or not REPO.fullmatch(value)
           or value.split("/")[1] in (".", "..") or value.endswith(".git") for value in result):
        raise BootstrapError("repositories must use OWNER/REPO names without .git")
    if len({value.split("/")[1].lower() for value in result}) != len(result):
        raise BootstrapError("repositories must have distinct destination names")
    return result


def _text(value, label, pattern, length=64):
    if not isinstance(value, str) or not value or len(value) > length or any(ord(c) < 32 for c in value):
        raise BootstrapError(f"invalid {label}")
    if pattern is not None and not pattern.fullmatch(value):
        raise BootstrapError(f"invalid {label}")
    return value


def validate_agent(agent) -> dict:
    if not isinstance(agent, dict):
        raise BootstrapError("each agent must be an object")
    extra = set(agent) - {"harness", "name", "topic", "model", "cost_center_id"}
    if extra:
        raise BootstrapError("unexpected agent fields")
    harness = agent.get("harness")
    if harness not in HARNESSES:
        raise BootstrapError("harness must be claude, codex or omp")
    name = _text(agent.get("name"), "name", NAME, 32)
    topic = _text(agent.get("topic"), "topic", NAME, 32)
    model = agent.get("model")
    if model is not None:
        model = _text(model, "model", MODEL)
    cost = agent.get("cost_center_id")
    if cost is not None:
        cost = _text(cost, "cost_center_id", re.compile(r"[a-z0-9][a-z0-9_-]{0,63}"))
    return {key: value for key, value in
            (("harness", harness), ("name", name), ("topic", topic), ("model", model),
             ("cost_center_id", cost)) if value is not None or key in ("harness", "name", "topic")}


def validate_payload(data) -> dict:
    """Reject unexpected fields. Secrets may be present; they never go into health."""
    if not isinstance(data, dict):
        raise BootstrapError("invalid bootstrap payload")
    allowed = {"job_id", "repositories", "primary_repo", "agents", "node", "hub_url",
               "service_account", "vault_ref", "hub_token", "token"}
    if set(data) - allowed:
        raise BootstrapError("invalid bootstrap payload")
    job_id = _text(data.get("job_id"), "job_id", re.compile(r"[0-9a-f]{32}"))
    repos = repositories(data.get("repositories"))
    primary = data.get("primary_repo") or repos[0]
    if primary not in repos:
        raise BootstrapError("primary_repo must be one of the selected repositories")
    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        raise BootstrapError("agents must be a non-empty list")
    if len({agent.get("name") for agent in agents}) != len(agents):
        raise BootstrapError("agent names must be unique on a box")
    node = data.get("node")
    hub_url = data.get("hub_url")
    if node is not None:
        node = _text(node, "node", NAME, 32)
    if hub_url is not None:
        hub_url = _text(hub_url, "hub_url", re.compile(r"https?://[A-Za-z0-9._:/-]+"), 200)
    return {
        "job_id": job_id, "repositories": repos, "primary_repo": primary,
        "agents": [validate_agent(agent) for agent in agents],
        **({} if node is None else {"node": node}),
        **({} if hub_url is None else {"hub_url": hub_url}),
        **{key: data[key] for key in SECRET_KEYS if key in data},
    }


def redact(text: str) -> str:
    """Keep diagnostics without retaining tokens or common secret forms."""
    text = re.sub(r"(?i)(https?://[^\s?]+)\?\S+", r"\1?<redacted>", text)
    text = re.sub(r"\b(?:sk-[\w-]{12,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b", "<redacted>", text)
    text = re.sub(r"(?i)((?:token|api[_-]?key|authorization|password|secret|credential|"
                  r"service[_-]?account)\s*[:=]\s*)(?:bearer\s+)?\S+", r"\1<redacted>", text)
    return text[-4000:]


def profile_presence(environ=None) -> dict:
    """Booleans only: a named profile is configured if any of its env vars is non-empty."""
    environ = os.environ if environ is None else environ
    return {name: any(isinstance(environ.get(key), str) and environ[key].strip() for key in keys)
            for name, keys in PROFILE_ENV.items()}


def skill_links(documents: Path, skills: Path) -> list[str]:
    """Portable SKILL.md links for selected clones, matching box-provision's sibling layout.

    Does not clone personal CLIs, install cron, or pair WhatsApp/Telegram.
    """
    skills.mkdir(parents=True, exist_ok=True)
    linked = []
    for repo in sorted(documents.iterdir() if documents.is_dir() else []):
        if not repo.is_dir() or not (repo / "SKILL.md").is_file():
            continue
        link = skills / repo.name
        if link.exists() or link.is_symlink():
            if link.is_symlink() and not link.exists():
                candidate = documents / os.path.basename(os.readlink(link))
                if candidate.is_dir():
                    link.unlink()
                    link.symlink_to(os.path.relpath(candidate, skills))
                    linked.append(repo.name)
            continue
        link.symlink_to(os.path.relpath(repo, skills))
        linked.append(repo.name)
    return linked


def worktree_path(documents: Path, name: str) -> Path:
    return documents / "workers" / _text(name, "name", NAME, 32)


def worktree_argv(repo_dir: Path, destination: Path) -> list[str]:
    """Fixed git argv; the destination is a validated name path, not a shell string."""
    return ["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(destination)]


def harness_argv(harness: str, model: str | None) -> list[str]:
    """Interactive harness without a user prompt so residency does not spend a model turn."""
    if harness not in HARNESSES:
        raise BootstrapError("harness must be claude, codex or omp")
    if harness == "omp":
        return ["omp", *([f"--model={model}"] if model else [])]
    return [harness, *(["--model", model] if model else [])]


def tmux_argv(session: str, cwd: Path, argv: list[str]) -> list[str]:
    """tmux runs argv directly; session is a validated identifier."""
    if not re.fullmatch(r"oneconv-[a-z0-9][a-z0-9-]{0,48}", session):
        raise BootstrapError("invalid tmux session")
    return ["tmux", "new-session", "-d", "-s", session, "-c", str(cwd), "-x", "200", "-y", "50", "--", *argv]


def box_exec_argv(sandbox: str, argv: list[str], timeout: int = 120) -> list[str]:
    """Boat joins exec arguments into a shell; quote every fixed argument."""
    if not re.fullmatch(r"bx_[A-Za-z0-9]+", sandbox):
        raise BootstrapError("invalid sandbox id")
    if not argv or any(not isinstance(part, str) for part in argv):
        raise BootstrapError("exec argv must be strings")
    return ["box", "exec", sandbox, "--timeout", str(timeout), "--json", "--no-update", "--", shlex.join(argv)]


def load_payload(path: Path) -> dict:
    """Read, validate, and delete the payload so a leftover file cannot leak secrets."""
    try:
        path.chmod(0o600)
        data = validate_payload(json.loads(path.read_text()))
    finally:
        path.unlink(missing_ok=True)
    return data


def health_document(payload: dict, *, clones: dict, skills: list, agents: list,
                    bridge: dict, profiles: dict, auth_needed: list) -> dict:
    """Whitelist booleans and identifiers; never forward provider output or secrets."""
    return {
        "job_id": payload["job_id"],
        "repositories": clones,
        "skills": list(skills),
        "agents": agents,
        "bridge": bridge,
        "profiles": {name: bool(profiles.get(name)) for name in PROFILE_ENV},
        "auth_needed": list(auth_needed),
        "capacity": {"idle_residency_verified_up_to": 10, "active_capacity_verified": False},
    }


def validate_health(data, payload) -> dict:
    if not isinstance(data, dict):
        raise BootstrapError("invalid worker health response")
    expected = {"job_id", "repositories", "skills", "agents", "bridge", "profiles", "auth_needed", "capacity"}
    if set(data) != expected:
        raise BootstrapError("invalid worker health response")
    if data["job_id"] != payload["job_id"]:
        raise BootstrapError("invalid worker health response")
    return data


def parse_exec_health(raw: str, payload: dict) -> dict:
    """Whitelist helper JSON rather than forwarding provider stdout."""
    data = json.loads(raw)
    if isinstance(data, dict) and "stdout" in data:
        try:
            response = json.loads(data["stdout"])
        except (TypeError, ValueError) as error:
            raise BootstrapError("invalid worker health response") from error
        if (data.get("success") is not True or data.get("exitCode") != 0 or data.get("stdoutTruncated") is not False
                or data.get("timedOut") is not False or data.get("launcherFailed") is not False
                or data.get("oomKilled") is not False):
            raise BootstrapError("worker command did not complete successfully")
        data = response
    return validate_health(data, payload)


def _clone_health(repo: str, documents: Path) -> dict:
    dest = documents / repo.split("/")[1]
    url = f"https://github.com/{repo}.git"
    clone = dest.is_dir() and (dest / ".git").exists()
    origin = ""
    if clone:
        result = subprocess.run(["git", "-C", str(dest), "remote", "get-url", "origin"],
                                capture_output=True, text=True, check=False)
        origin = result.stdout.strip()
    return {"permission": clone or dest.exists(), "clone": clone and origin in ("", url, url.removesuffix(".git"))}


def prepare_layout(payload: dict, documents: Path, skills: Path) -> dict:
    """Create worktree directories and skill links after clones exist. No harness is started here."""
    documents.mkdir(parents=True, exist_ok=True)
    primary = documents / payload["primary_repo"].split("/")[1]
    clones = {repo: _clone_health(repo, documents) for repo in payload["repositories"]}
    linked = skill_links(documents, skills)
    agents = []
    for agent in payload["agents"]:
        destination = worktree_path(documents, agent["name"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if primary.is_dir() and not destination.exists():
            subprocess.run(worktree_argv(primary, destination), capture_output=True, text=True, check=False)
        session = f"oneconv-{agent['harness']}-{agent['name']}"
        agents.append({
            "harness": agent["harness"], "name": agent["name"], "topic": agent["topic"],
            "model": agent.get("model"), "cost_center_id": agent.get("cost_center_id"),
            "worktree": str(destination), "tmux": session,
            "address": None, "registered": False, "published": False, "topics": [],
            "auth": "needed",
        })
    return {"clones": clones, "skills": linked, "agents": agents,
            "harness_argv": {agent["name"]: harness_argv(agent["harness"], agent.get("model"))
                             for agent in payload["agents"]}}


def apply_auth(agents: list, profiles: dict) -> list[str]:
    """Mark harnesses that cannot be ready. Codex native auth is absent unless CODEX_API_KEY is set."""
    needed = []
    for agent in agents:
        harness = agent["harness"]
        configured = (
            (harness == "codex" and profiles.get("codex_native"))
            or (harness in ("claude", "omp") and (profiles.get("claude_subscription") or profiles.get("openrouter")))
        )
        agent["auth"] = "configured" if configured else "needed"
        if not configured:
            needed.append(harness)
    return list(dict.fromkeys(needed))


def _main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "--worker-payload":
        print(json.dumps({"error": "usage"}), file=sys.stderr)
        return 2
    payload = load_payload(Path(argv[2]))
    documents = Path.home() / "Documents"
    skills = Path.home() / ".claude" / "skills"
    layout = prepare_layout(payload, documents, skills)
    profiles = profile_presence()
    auth_needed = apply_auth(layout["agents"], profiles)
    document = health_document(
        payload, clones=layout["clones"], skills=layout["skills"], agents=layout["agents"],
        bridge={"running": False, "published": False}, profiles=profiles, auth_needed=auth_needed,
    )
    json.dump(document, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main(sys.argv))
    except (OSError, ValueError, json.JSONDecodeError):
        json.dump({"error": "bootstrap"}, sys.stdout)
        sys.stdout.write("\n")
        raise SystemExit(1) from None
