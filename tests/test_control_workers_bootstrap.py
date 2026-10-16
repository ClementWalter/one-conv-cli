"""Bootstrap helper: selected clones, portable skills, no tokens in logs or health."""

import json
from pathlib import Path

import pytest

from one_conv.control import worker_bootstrap as bootstrap

JOB = "a" * 32
REPO = "Example/one-conv-cli"
AGENT = {"harness": "claude", "name": "reviewer", "topic": "slack-for-agents", "cost_center_id": "jack"}


def payload(**extra):
    data = {"job_id": JOB, "repositories": [REPO], "primary_repo": REPO, "agents": [AGENT]}
    data.update(extra)
    return data


def test_repositories_reject_unsafe_names():
    for repo in ("../escape", "Example/..", "Example/.", "Example/repo.git", "Example/repo/other",
                 "https://github.com/Example/repo", "Example/repo;id", "Example/repo\n"):
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.repositories([repo])


def test_payload_rejects_extra_secret_fields():
    with pytest.raises(bootstrap.BootstrapError, match="invalid bootstrap payload"):
        bootstrap.validate_payload({**payload(), "GITHUB_TOKEN": "synthetic-github-secret"})


def test_payload_keeps_hub_token_out_of_health():
    data = bootstrap.validate_payload({**payload(), "hub_token": "secret-hub-token"})
    document = bootstrap.health_document(
        data, clones={REPO: {"permission": True, "clone": True}}, skills=["one-conv-cli"],
        agents=[{"harness": "claude", "name": "reviewer", "topic": "slack-for-agents",
                 "address": "claude:abc", "registered": True, "published": True,
                 "topics": ["slack-for-agents"], "auth": "configured"}],
        bridge={"running": True, "published": True},
        profiles={"claude_subscription": True, "openrouter": False, "codex_native": False},
        auth_needed=[])
    dumped = json.dumps(document)
    assert "secret-hub-token" not in dumped
    assert set(document) == {"job_id", "repositories", "skills", "agents", "bridge", "profiles",
                             "auth_needed", "capacity"}
    assert document["capacity"]["active_capacity_verified"] is False


def test_redact_strips_tokens():
    text = bootstrap.redact("token=secret-hub-token Authorization: Bearer abc.def.ghi sk-abcdefghijklmnopqrst")
    assert "secret-hub-token" not in text
    assert "sk-abcdefghijklmnopqrst" not in text


def test_skill_links_selected_clones_only(tmp_path):
    documents = tmp_path / "Documents"
    skills = tmp_path / ".claude" / "skills"
    repo = documents / "one-conv-cli"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text("# skill\n")
    (documents / "whatsapp-cli").mkdir()
    assert bootstrap.skill_links(documents, skills) == ["one-conv-cli"]
    assert (skills / "one-conv-cli").is_symlink()
    assert not (skills / "whatsapp-cli").exists()
    again = bootstrap.skill_links(documents, skills)
    assert again == []


def test_repoint_broken_skill_symlink(tmp_path):
    documents = tmp_path / "Documents"
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    repo = documents / "one-conv-cli"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text("# skill\n")
    (skills / "one-conv-cli").symlink_to("/mac/only/one-conv-cli")
    assert bootstrap.skill_links(documents, skills) == ["one-conv-cli"]
    assert (skills / "one-conv-cli").resolve() == repo.resolve()


def test_harness_and_tmux_argv_are_literal():
    argv = bootstrap.harness_argv("claude", "sonnet")
    assert argv == ["claude", "--model", "sonnet"]
    session = "oneconv-claude-reviewer"
    tmux = bootstrap.tmux_argv(session, Path("/work"), argv)
    assert tmux[:3] == ["tmux", "new-session", "-d"]
    assert "--" in tmux
    assert tmux[-3:] == ["claude", "--model", "sonnet"]
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.tmux_argv("rm -rf", Path("/work"), argv)


def test_box_exec_quotes_every_argument():
    argv = bootstrap.box_exec_argv("bx_abc", ["python3", "/tmp/one-conv-worker-bootstrap.py",
                                              "--worker-payload", "/tmp/x.json"])
    assert argv[:3] == ["box", "exec", "bx_abc"]
    assert argv[-2] == "--"
    assert "$(reboot)" not in argv[-1] or False
    assert "python3" in argv[-1]


def test_load_payload_unlinks(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload(hub_token="secret-hub-token")))
    loaded = bootstrap.load_payload(path)
    assert loaded["hub_token"] == "secret-hub-token"
    assert not path.exists()


def test_profile_presence_is_boolean_only():
    profiles = bootstrap.profile_presence({
        "CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth", "OPENROUTER_API_KEY": "",
        "CODEX_API_KEY": None,
    })
    assert profiles == {"claude_subscription": True, "openrouter": False, "codex_native": False}


def test_apply_auth_codex_needed_without_native_key():
    agents = [{"harness": "codex", "auth": "needed"}, {"harness": "claude", "auth": "needed"}]
    needed = bootstrap.apply_auth(agents, {"claude_subscription": True, "openrouter": False, "codex_native": False})
    assert needed == ["codex"]
    assert agents[0]["auth"] == "needed"
    assert agents[1]["auth"] == "configured"


def test_prepare_layout_worktrees(tmp_path):
    documents = tmp_path / "Documents"
    repo = documents / "one-conv-cli"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "SKILL.md").write_text("# skill\n")
    layout = bootstrap.prepare_layout(bootstrap.validate_payload(payload()), documents, tmp_path / "skills")
    assert layout["agents"][0]["tmux"] == "oneconv-claude-reviewer"
    assert layout["harness_argv"]["reviewer"] == ["claude"]
    assert "one-conv-cli" in layout["skills"]


def test_health_envelope_drops_diagnostics():
    health = bootstrap.health_document(
        payload(), clones={REPO: {"permission": True, "clone": True}}, skills=[],
        agents=[], bridge={"running": False, "published": False},
        profiles={"claude_subscription": False, "openrouter": False, "codex_native": False},
        auth_needed=["claude"])
    envelope = {"stdout": json.dumps(health), "stderr": "token=secret-hub-token", "exitCode": 0,
                "success": True, "stdoutTruncated": False, "timedOut": False,
                "launcherFailed": False, "oomKilled": False}
    parsed = bootstrap.parse_exec_health(json.dumps(envelope), payload())
    assert parsed == health
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.parse_exec_health(json.dumps({**health, "token": "secret"}), payload())


def test_main_usage_error():
    assert bootstrap._main(["worker_bootstrap.py"]) == 2
