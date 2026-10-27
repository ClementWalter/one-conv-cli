#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "pytest-xdist==3.8.0",
#     "click==8.3.3",
#     "curl_cffi==0.13.0",
#     "pycryptodome==3.23.0",
#     "mcp==2.2.0",
#     "websockets==17.2",
#     "starlette==1.8.0",
#     "uvicorn==0.54.0",
#     "sqlalchemy==2.1.4",
#     "pydantic==2.12.5",
#     "httpx==0.28.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Boat adapter against a stub `box` that replays output captured from real Boat sandboxes."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from one_conv.live import boat, router
from one_conv.live.model import LiveSession, envelope

SANDBOX = "bx_test1234"
IDLE = "2ab1916c-4b93-41ca-b453-9f3a5c92fb24"
BUSY = "24c345be-0000-4000-8000-000000000002"

STUB = r'''#!PYTHON
"""Stand-in for the Boat CLI: records argv, answers from canned run-2 samples."""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["BOX_STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
command = args[0]
if command == "list":
    print(json.dumps({"sandboxes": [{"id": "SANDBOX", "name": "test box", "state": "running"}]}))
elif command == "conversations":
    for convo, running in (("IDLE", False), ("BUSY", True)):
        print(json.dumps({"event": "conversation", "data": {"id": convo, "running": running,
              "lastHarness": "claude-code", "lastPromptPreview": "You are a remote test agent"}}))
elif command == "prompt":
    mode = os.environ.get("BOX_STUB_PROMPT", "accept")
    if mode == "lost":
        sys.exit(1)  # The prompt may have been forwarded; the acknowledgement never comes back.
    if mode == "refuse":
        print(json.dumps({"code": "provider_not_configured", "error": "Codex is not set up", "event": "error", "status": 409}))
        sys.exit(1)
    import time
    time.sleep(float(os.environ.get("BOX_STUB_DELAY", "0")))
    print(json.dumps({"event": "chat", "final": True, "data": {"conversationId": args[3], "type": "prompt",
          "timestamp": 1, "data": {"prompt": args[-1], "status": "finished"}}}), flush=True)
elif command == "steer":
    steer = os.environ.get("BOX_STUB_STEER", "steered")
    if steer == "lost":
        sys.exit(1)  # The steer may have been forwarded; the acknowledgement never comes back.
    if steer == "slow":
        import time
        time.sleep(float(os.environ.get("BOX_STUB_DELAY", "1.5")))
    if steer == "ended":
        print(json.dumps({"code": "no_running_turn", "error": "The last turn already ended", "event": "error", "status": 409}))
        sys.exit(1)
    print(json.dumps({"conversationId": args[3], "mode": "native", "status": "steered"}))
elif command == "events":
    rows = [
        {"event": "chat", "final": True, "data": {"type": "prompt", "timestamp": 1, "data": {"prompt": "question"}}},
        {"event": "chat", "final": False, "data": {"type": "response", "timestamp": 2, "data": {"content": "Rea"}}},
        {"event": "chat", "final": True, "data": {"type": "response", "timestamp": 3, "data": {"content": "Ready."}}},
        {"event": "chat", "final": True, "data": {"type": "response", "timestamp": 4,
         "data": {"content": "withdrawn", "is_reverted": True}}},
        {"event": "chat", "final": True, "data": {"type": "response", "timestamp": 5, "data": {"content": ""}}},
    ]
    for row in rows:
        print(json.dumps(row))
'''


@pytest.fixture
def box(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "box"
    script.write_text(STUB.replace("PYTHON", sys.executable).replace("SANDBOX", SANDBOX)
                      .replace('"IDLE"', json.dumps(IDLE)).replace('"BUSY"', json.dumps(BUSY)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "box-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOX_STUB_LOG", str(log))
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    return log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_lists_each_conversation_with_its_address(box):
    assert sorted(s.address for s in boat.list_sessions()) == sorted(
        [f"boat:{SANDBOX}/{IDLE}", f"boat:{SANDBOX}/{BUSY}"])


@pytest.mark.parametrize(("conversation", "status"), [(IDLE, "idle"), (BUSY, "busy")])
def test_running_flag_is_the_busy_state(box, conversation, status):
    assert {s.id: s.status for s in boat.list_sessions()}[f"{SANDBOX}/{conversation}"] == status


def test_listing_everything_does_not_call_boat(box):
    router.list_all()
    assert calls(box) == []


def test_delivery_passes_the_text_as_one_argument(box):
    text = '<one-conv-message from="user">\nline "two" $(echo PWNED) x;y\n</one-conv-message>'
    boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), text)
    assert calls(box)[-1][-1] == text


def test_delivery_resumes_the_target_conversation(box):
    boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>")
    assert calls(box)[-1][:4] == ["prompt", SANDBOX, "--resume", IDLE]


def test_busy_conversation_is_steered(box):
    status = boat.deliver(LiveSession("boat", f"{SANDBOX}/{BUSY}", status="busy"), "<one-conv-message>hi</one-conv-message>")
    assert status.startswith("steered into the running turn")


def test_steer_passes_the_text_as_one_argument(box):
    text = '<one-conv-message from="claude:c1">\nline "two" $(echo PWNED) x;y\n</one-conv-message>'
    boat.deliver(LiveSession("boat", f"{SANDBOX}/{BUSY}", status="busy"), text)
    assert calls(box)[-1] == ["steer", SANDBOX, "--convo", BUSY, "--json", "--no-update", text]


def test_steer_after_the_turn_ended_falls_back_to_prompt(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_STEER", "ended")
    boat.deliver(LiveSession("boat", f"{SANDBOX}/{BUSY}", status="busy"), "<one-conv-message>hi</one-conv-message>")
    assert calls(box)[-1][:4] == ["prompt", SANDBOX, "--resume", BUSY]


def test_refused_prompt_reports_boats_reason(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_PROMPT", "refuse")
    with pytest.raises(RuntimeError, match="provider_not_configured"):
        boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>")


def test_send_to_boat_asks_for_an_answer_in_place(box):
    router.send(f"boat:{SANDBOX}/{IDLE}", "status?", LiveSession("claude", "c1"))
    assert "Answer in this conversation" in calls(box)[-1][-1]


def test_send_to_boat_offers_no_reply_tool(box):
    router.send(f"boat:{SANDBOX}/{IDLE}", "status?", LiveSession("claude", "c1"))
    assert "send_message" not in calls(box)[-1][-1]


def test_fyi_to_boat_is_refused(box):
    with pytest.raises(router.LiveError, match="Boat"):
        router.send(f"boat:{SANDBOX}/{IDLE}", "merged", LiveSession("claude", "c1"), fyi=True)


def test_pull_keeps_final_unreverted_turns_with_text(box):
    assert [(t["role"], t["text"]) for t in boat.pull(f"{SANDBOX}/{IDLE}")] == [
        ("user", "question"), ("assistant", "Ready.")]


def test_pull_asks_for_one_conversation(box):
    boat.pull(f"{SANDBOX}/{IDLE}")
    assert calls(box)[-1][:4] == ["events", SANDBOX, "--convo", IDLE]


def test_pull_does_not_follow_the_stream(box):
    boat.pull(f"{SANDBOX}/{IDLE}")
    assert "-f" not in calls(box)[-1] and "--follow" not in calls(box)[-1]


@pytest.mark.parametrize("ident", ["bx_only", "/convo", "bx/"])
def test_malformed_boat_address_is_rejected(ident):
    with pytest.raises(ValueError):
        boat.split(ident)


def test_inline_envelope_names_where_to_answer():
    assert envelope("hi", LiveSession("claude", "c1"), reply="inline").endswith(
        "Answer in this conversation; the sender reads your reply back from here.")


@pytest.fixture
def slow_box(box, monkeypatch):
    """Boat echoes the prompt only after the send has stopped waiting for it."""
    monkeypatch.setenv("BOX_STUB_DELAY", "1.5")
    monkeypatch.setattr(boat, "ACCEPT_SECONDS", 0.3)
    return box


def _op(status: str) -> str:
    return status.split("(op ")[1].split(")")[0]


def test_unconfirmed_prompt_is_pending_not_failed(slow_box):
    status = boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>")
    assert status.startswith("pending:")


def test_pending_prompt_says_not_to_resend(slow_box):
    status = boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>")
    assert "Do not resend" in status


def test_second_send_while_first_is_pending_is_refused(slow_box):
    target = LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle")
    boat.deliver(target, "<one-conv-message>hi</one-conv-message>")
    with pytest.raises(RuntimeError, match="no confirmed outcome"):
        boat.deliver(target, "<one-conv-message>hi</one-conv-message>")


@pytest.fixture
def launches(monkeypatch):
    """`box prompt` processes started by this test, counted in this process.

    The slow stub may not have written its own call log yet when the test checks,
    so its log cannot tell a missing second prompt from one that has not started.
    """
    started = []
    real_popen = subprocess.Popen

    def counting_popen(args, *rest, **kwargs):
        started.append(list(args))
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(boat.subprocess, "Popen", counting_popen)
    return started


def test_second_send_while_first_is_pending_starts_no_prompt(slow_box, launches):
    target = LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle")
    boat.deliver(target, "<one-conv-message>hi</one-conv-message>")
    with pytest.raises(RuntimeError):
        boat.deliver(target, "<one-conv-message>hi</one-conv-message>")
    assert [args[1] for args in launches].count("prompt") == 1


def test_pending_prompt_later_reads_as_accepted(slow_box):
    op = _op(boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>"))
    deadline = time.monotonic() + 10
    while boat.operation(op)["state"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert boat.operation(op)["state"] == "accepted"


def test_pending_send_uses_a_rate_slot(slow_box):
    router.send(f"boat:{SANDBOX}/{IDLE}", "status?", LiveSession("claude", "c1"))
    assert (Path(os.environ["ONE_CONV_LIVE_DIR"]) / "sent.jsonl").exists()


def test_unknown_operation_is_reported(box):
    with pytest.raises(RuntimeError, match="no Boat send operation"):
        boat.operation("missing")


def test_pull_timeout_is_an_actionable_error(box, monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("box", 120)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(RuntimeError, match="no answer within"):
        boat.pull(f"{SANDBOX}/{IDLE}")


def test_inline_envelope_carries_the_reply_attribute():
    assert 'reply="inline"' in envelope("hi", LiveSession("claude", "c1"), reply="inline")


@pytest.fixture
def lost_box(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_PROMPT", "lost")
    return box


def test_exit_without_acknowledgement_is_unconfirmed(lost_box):
    status = boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>")
    assert status.startswith("unconfirmed:")


def test_unconfirmed_send_uses_a_rate_slot(lost_box):
    router.send(f"boat:{SANDBOX}/{IDLE}", "status?", LiveSession("claude", "c1"))
    assert (Path(os.environ["ONE_CONV_LIVE_DIR"]) / "sent.jsonl").exists()


def test_unconfirmed_send_blocks_a_second_prompt(lost_box):
    target = LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle")
    boat.deliver(target, "<one-conv-message>hi</one-conv-message>")
    with pytest.raises(RuntimeError, match="no confirmed outcome"):
        boat.deliver(target, "<one-conv-message>hi</one-conv-message>")


def test_unconfirmed_status_says_not_to_resend(lost_box):
    op = _op(boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>"))
    assert "Do not resend" in boat.operation(op)["help"]


def test_resolving_an_unconfirmed_send_allows_another(lost_box):
    target = LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle")
    boat.resolve_operation(_op(boat.deliver(target, "<one-conv-message>hi</one-conv-message>")))
    boat.deliver(target, "<one-conv-message>hi</one-conv-message>")
    assert [c[0] for c in calls(lost_box)].count("prompt") == 2


def test_only_an_unconfirmed_send_can_be_resolved(box):
    boat.deliver(LiveSession("boat", f"{SANDBOX}/{IDLE}", status="idle"), "<one-conv-message>hi</one-conv-message>")
    [record] = (Path(os.environ["ONE_CONV_LIVE_DIR"]) / "boat" / "ops").glob("*[0-9a-f].json")
    with pytest.raises(RuntimeError, match="only an unconfirmed send"):
        boat.resolve_operation(record.stem)


@pytest.mark.parametrize("op", ["../../etc/x", "ABCDEF123456", "abc", "0123456789abcdef"])
def test_operation_id_must_be_twelve_hex_characters(box, op):
    with pytest.raises(RuntimeError, match="no Boat send operation"):
        boat.operation(op)


BUSY_TARGET = LiveSession("boat", f"{SANDBOX}/{BUSY}", status="busy")
TEXT = "<one-conv-message>hi</one-conv-message>"


def test_lost_steer_acknowledgement_is_unconfirmed(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_STEER", "lost")
    assert boat.deliver(BUSY_TARGET, TEXT).startswith("unconfirmed: box steer")


def test_lost_steer_blocks_a_second_steer(box, monkeypatch, launches):
    monkeypatch.setenv("BOX_STUB_STEER", "lost")
    boat.deliver(BUSY_TARGET, TEXT)
    with pytest.raises(RuntimeError, match="no confirmed outcome"):
        boat.deliver(BUSY_TARGET, TEXT)
    assert [args[1] for args in launches].count("steer") == 1


def test_lost_steer_uses_a_rate_slot(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_STEER", "lost")
    router.send(f"boat:{SANDBOX}/{BUSY}", "status?", LiveSession("claude", "c1"))
    assert (Path(os.environ["ONE_CONV_LIVE_DIR"]) / "sent.jsonl").exists()


def test_slow_steer_is_pending(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_STEER", "slow")
    monkeypatch.setattr(boat, "ACCEPT_SECONDS", 0.3)
    assert boat.deliver(BUSY_TARGET, TEXT).startswith("pending: Boat has not confirmed the steer")


def test_slow_steer_later_reads_as_accepted(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_STEER", "slow")
    monkeypatch.setattr(boat, "ACCEPT_SECONDS", 0.3)
    op = _op(boat.deliver(BUSY_TARGET, TEXT))
    deadline = time.monotonic() + 10
    while boat.operation(op)["state"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert boat.operation(op)["state"] == "accepted"


def test_steer_into_an_ended_turn_does_not_block_later_sends(box, monkeypatch):
    monkeypatch.setenv("BOX_STUB_STEER", "ended")
    boat.deliver(BUSY_TARGET, TEXT)
    assert boat._pending_for(BUSY_TARGET.address) is None


def test_topic_fan_out_keeps_an_unknown_outcome(box, monkeypatch):
    from one_conv.live import team
    monkeypatch.setenv("BOX_STUB_STEER", "lost")
    team.join("release", f"boat:{SANDBOX}/{BUSY}")
    receipt = router.send("#release", "status?", LiveSession("claude", "c1"))
    assert receipt["deliveries"][0]["status"].startswith("unconfirmed")
