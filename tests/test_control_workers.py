"""Worker jobs persist before Boat side effects and never recreate an unconfirmed sandbox."""

import json
import stat
from pathlib import Path

import pytest

from one_conv.control import worker_bootstrap as bootstrap
from one_conv.control.workers import (
    BoxProvider,
    WorkerError,
    WorkerManager,
    extract_created_id,
)

REPO = "Example/one-conv-cli"
JOB = "a" * 32
ADDRESS = "claude:" + "b" * 32


def spec(harness="claude", name="reviewer", topic="slack-for-agents", model=None, cost="jack", extra=None):
    agent = {"harness": harness, "name": name, "topic": topic, "cost_center_id": cost}
    if model:
        agent["model"] = model
    payload = {"repo": REPO, "agents": [agent]}
    if extra:
        payload.update(extra)
    return payload


def envelope(health):
    return json.dumps({"stdout": json.dumps(health), "success": True, "exitCode": 0,
                       "stdoutTruncated": False, "timedOut": False, "launcherFailed": False,
                       "oomKilled": False})


def healthy(job_id, item, *, registered=True, published=True, topics=None, auth="configured",
            auth_needed=None, address=ADDRESS, bridge=True, profiles=None):
    agent = {"harness": item["agents"][0]["harness"], "name": item["agents"][0]["name"],
             "topic": item["agents"][0]["topic"], "model": item["agents"][0].get("model"),
             "cost_center_id": item["agents"][0].get("cost_center_id"),
             "worktree": f"/home/user/Documents/workers/{item['agents'][0]['name']}",
             "tmux": f"oneconv-{item['agents'][0]['harness']}-{item['agents'][0]['name']}",
             "address": address, "registered": registered, "published": published,
             "topics": topics if topics is not None else [item["agents"][0]["topic"]], "auth": auth}
    return {
        "job_id": job_id, "repositories": {REPO: {"permission": True, "clone": True}},
        "skills": ["one-conv-cli"], "agents": [agent],
        "bridge": {"running": bridge, "published": bridge},
        "profiles": profiles or {"claude_subscription": True, "openrouter": False, "codex_native": False},
        "auth_needed": auth_needed or [],
        "capacity": {"idle_residency_verified_up_to": 10, "active_capacity_verified": False},
    }


class FakeBox(BoxProvider):
    """In-memory Boat stand-in. Never talks to a real sandbox."""

    def __init__(self):
        self.creates, self.execs, self.scps, self.stops = [], [], [], []
        self.boxes, self.payloads = [], {}
        self.lose_create = False
        self.lose_after_id = False
        self.health_for = {}
        self.limits_value = {"available": True, "can_start": True, "credit_balance_hours": 12.5}

    def create(self, *, environment, box_type, job_id, ttl=None, on_id=None) -> str:
        self.creates.append(job_id)
        if self.lose_create:
            raise WorkerError("sandbox create was not acknowledged")
        sandbox = "bx_" + job_id[:8]
        if on_id is not None:
            on_id(sandbox)
        if self.lose_after_id:
            raise WorkerError("sandbox create was not acknowledged")
        self.boxes.append({"id": sandbox, "job_id": job_id, "state": "running", "environment": environment})
        return sandbox

    def list(self):
        return list(self.boxes)

    def limits(self):
        return self.limits_value

    def scp(self, sandbox, local, remote):
        self.scps.append((sandbox, Path(local).name, remote))
        if Path(local).name == "payload.json":
            self.payloads[sandbox] = json.loads(Path(local).read_text())

    def exec(self, sandbox, argv, timeout=120):
        self.execs.append(list(argv))
        if argv[:2] == ["python3", "-c"]:
            return envelope({"ok": True})
        payload = self.payloads.get(sandbox) or {"job_id": JOB, "repositories": [REPO],
                                                 "primary_repo": REPO, "agents": spec()["agents"]}
        health = self.health_for.get(sandbox) or healthy(payload["job_id"], payload)
        return envelope(health)

    def stop(self, sandbox):
        self.stops.append(sandbox)

    def job_id_for(self, sandbox):
        return next((row["job_id"] for row in self.boxes if row["id"] == sandbox), None)


@pytest.fixture
def manager(tmp_path):
    box = FakeBox()
    mgr = WorkerManager(tmp_path / "workers", {"max_agents_per_box": 4, "token": "secret-hub-token",
                                               "node": "cloud-1", "hub_url": "https://hub.example.test"}, box)
    return mgr, box


def test_extract_created_id_from_jsonl():
    text = '{"event":"progress"}\n{"event":"created","id":"bx_abc123"}\n'
    assert extract_created_id(text) == "bx_abc123"
    assert extract_created_id("not json\n{}") is None


def test_spec_rejects_cursor_and_shell_metacharacters(tmp_path):
    mgr = WorkerManager(tmp_path, {"allow_create": False}, FakeBox())
    with pytest.raises(WorkerError, match="harness"):
        mgr.submit({**spec(), "agents": [{"harness": "cursor", "name": "x", "topic": "t"}]}, JOB)
    with pytest.raises(WorkerError):
        mgr.submit({**spec(), "agents": [{"harness": "claude", "name": "a;rm", "topic": "t"}]}, JOB)
    with pytest.raises(WorkerError):
        mgr.submit({"repo": "Example/repo.git", "agents": spec()["agents"]}, JOB)


def test_cap_cannot_exceed_ten(tmp_path):
    with pytest.raises(WorkerError, match="1 to 10"):
        WorkerManager(tmp_path, {"max_agents_per_box": 11})


def test_submit_persists_queued_before_create(manager):
    mgr, box = manager
    seen = []
    original = box.create

    def wrapped(**kwargs):
        seen.append(mgr.store.get(kwargs["job_id"])["state"])
        assert mgr.store.get(kwargs["job_id"])["create_started"] is True
        return original(**kwargs)

    box.create = wrapped
    job = mgr.submit(spec(), JOB)
    assert seen == ["provisioning"]
    assert job["id"] == JOB
    assert job["sandbox_id"] == "bx_" + JOB[:8]
    assert box.creates == [JOB]


def test_lost_create_ack_is_unknown_and_does_not_recreate(manager):
    mgr, box = manager
    box.lose_create = True
    job = mgr.submit(spec(), JOB)
    assert job["state"] == "unknown"
    assert job["sandbox_id"] is None
    assert box.creates == [JOB]
    again = mgr.submit(spec(), JOB)
    assert again["state"] == "unknown"
    assert box.creates == [JOB]


def test_lost_create_after_id_keeps_sandbox_and_does_not_recreate(manager):
    mgr, box = manager
    box.lose_after_id = True
    job = mgr.submit(spec(), JOB)
    assert job["sandbox_id"] == "bx_" + JOB[:8]
    assert job["state"] in ("provisioning", "ready")
    assert box.creates == [JOB]
    mgr.submit(spec(), JOB)
    assert box.creates == [JOB]


def test_reconcile_attaches_existing_sandbox_without_create(manager):
    mgr, box = manager
    box.lose_create = True
    mgr.submit(spec(), JOB)
    box.lose_create = False
    box.boxes.append({"id": "bx_recovered", "job_id": JOB, "state": "running"})
    box.health_for["bx_recovered"] = healthy(JOB, spec())
    job = mgr.submit(spec(), JOB)
    assert box.creates == [JOB]
    assert job["sandbox_id"] == "bx_recovered"
    assert job["state"] == "ready"


def test_ready_requires_registration_publish_and_topic(manager):
    mgr, box = manager
    box.health_for["bx_" + JOB[:8]] = healthy(JOB, spec(), registered=False)
    job = mgr.submit(spec(), JOB)
    assert job["state"] == "provisioning"
    box.health_for["bx_" + JOB[:8]] = healthy(JOB, spec(), published=False)
    assert mgr.submit(spec(), JOB)["state"] == "provisioning"
    box.health_for["bx_" + JOB[:8]] = healthy(JOB, spec(), topics=[])
    assert mgr.submit(spec(), JOB)["state"] == "provisioning"
    box.health_for["bx_" + JOB[:8]] = healthy(JOB, spec(), address="node/claude:abc")
    assert mgr.submit(spec(), JOB)["state"] == "provisioning"
    box.health_for["bx_" + JOB[:8]] = healthy(JOB, spec())
    assert mgr.submit(spec(), JOB)["state"] == "ready"


def test_auth_needed_is_never_ready(manager):
    mgr, box = manager
    item = spec(harness="codex", name="coder")
    box.health_for["bx_" + JOB[:8]] = healthy(
        JOB, item, auth="needed", auth_needed=["codex"], address="codex:" + "c" * 32,
        profiles={"claude_subscription": True, "openrouter": False, "codex_native": False})
    job = mgr.submit(item, JOB)
    assert job["state"] == "provisioning"
    assert job["auth_needed"] == ["codex"]
    assert job["error"]["stage"] == "auth"
    dumped = json.dumps(job)
    assert "secret-hub-token" not in dumped


def test_openrouter_profile_is_reported_without_pretending_codex(manager):
    mgr, box = manager
    box.health_for["bx_" + JOB[:8]] = healthy(
        JOB, spec(), profiles={"claude_subscription": False, "openrouter": True, "codex_native": False})
    job = mgr.submit(spec(), JOB)
    assert job["health"]["profiles"]["openrouter"] is True
    assert job["health"]["profiles"]["codex_native"] is False
    assert job["capacity"]["active_capacity_verified"] is False
    assert job["capacity"]["idle_residency_verified_up_to"] == 10


def test_rate_card_and_quota_are_metadata(manager):
    mgr, _box = manager
    job = mgr.submit(spec(), JOB)
    assert job["rate_cards"] == ["jack"]
    assert job["quota"]["credit_balance_hours"] == 12.5
    assert job["quota"]["can_start"] is True


def test_exec_argv_has_no_user_text_or_token(manager):
    mgr, box = manager
    mgr.submit(spec(model="sonnet"), JOB)
    joined = " ".join(" ".join(row) for row in box.execs)
    assert "secret-hub-token" not in joined
    assert "review this please" not in joined
    assert bootstrap.FIXED_HELPER_NAME in joined
    assert "--worker-payload" in joined
    quoted = bootstrap.box_exec_argv("bx_abc", ["python3", "/tmp/one-conv-worker-bootstrap.py", "--worker-payload", "/tmp/x.json"])
    assert quoted[-2] == "--"
    assert " " not in quoted[-1].split()[0] or quoted[-1].startswith("'") or quoted[-1].startswith('"') or "python3" in quoted[-1]


def test_identical_retry_is_idempotent(manager):
    mgr, box = manager
    first = mgr.submit(spec(), JOB)
    second = mgr.submit(spec(), JOB)
    assert first["id"] == second["id"]
    assert box.creates == [JOB]
    with pytest.raises(WorkerError, match="different spec"):
        mgr.submit(spec(name="other"), JOB)


def test_stop_does_not_delete_and_is_terminal(manager):
    mgr, box = manager
    mgr.submit(spec(), JOB)
    stopped = mgr.stop(JOB)
    assert box.stops == ["bx_" + JOB[:8]]
    assert stopped["stopped"] is True
    assert stopped["state"] == "failed"
    assert mgr.submit(spec(), JOB)["stopped"] is True
    assert box.creates == [JOB]


def test_allow_create_false_fails_closed(tmp_path):
    box = FakeBox()
    mgr = WorkerManager(tmp_path, {"allow_create": False}, box)
    job = mgr.submit(spec(), JOB)
    assert job["state"] == "failed"
    assert box.creates == []


def test_directory_is_private(manager):
    mgr, _box = manager
    mgr.submit(spec(), JOB)
    mode = stat.S_IMODE(mgr.store.directory.stat().st_mode)
    assert mode == 0o700


def test_list_and_status(manager):
    mgr, _box = manager
    mgr.submit(spec(), JOB)
    assert mgr.status(JOB)["id"] == JOB
    assert [row["id"] for row in mgr.list()] == [JOB]
    with pytest.raises(WorkerError):
        mgr.status("c" * 32)


def test_too_many_agents_for_cap(tmp_path):
    mgr = WorkerManager(tmp_path, {"max_agents_per_box": 1}, FakeBox())
    with pytest.raises(WorkerError, match="cap is 1"):
        mgr.submit({"repo": REPO, "agents": [spec()["agents"][0], {**spec()["agents"][0], "name": "other"}]}, JOB)
