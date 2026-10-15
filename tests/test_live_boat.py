"""Boat adapter against a stub `box` that replays output captured from real Boat sandboxes."""

import json
import os
import stat
import sys
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
    if mode == "refuse":
        print(json.dumps({"code": "provider_not_configured", "error": "Codex is not set up", "event": "error", "status": 409}))
        sys.exit(1)
    print(json.dumps({"event": "chat", "final": True, "data": {"conversationId": args[3], "type": "prompt",
          "timestamp": 1, "data": {"prompt": args[-1], "status": "finished"}}}), flush=True)
elif command == "events":
    rows = [
        {"event": "chat", "final": True, "data": {"type": "prompt", "timestamp": 1, "data": {"prompt": "question"}}},
        {"event": "chat", "final": False, "data": {"type": "response", "timestamp": 2, "data": {"content": "Rea"}}},
        {"event": "chat", "final": True, "data": {"type": "response", "timestamp": 3, "data": {"content": "Ready."}}},
        {"event": "chat", "final": True, "data": {"type": "response", "timestamp": 4,
         "data": {"content": "withdrawn", "is_reverted": True}}},
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


def test_busy_conversation_is_refused_without_steering(box):
    with pytest.raises(RuntimeError, match="running a turn"):
        boat.deliver(LiveSession("boat", f"{SANDBOX}/{BUSY}", status="busy"), "<one-conv-message>hi</one-conv-message>")


def test_busy_refusal_calls_no_box_command(box):
    with pytest.raises(RuntimeError):
        boat.deliver(LiveSession("boat", f"{SANDBOX}/{BUSY}", status="busy"), "<one-conv-message>hi</one-conv-message>")
    assert calls(box) == []


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


def test_pull_keeps_final_unreverted_turns(box):
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
