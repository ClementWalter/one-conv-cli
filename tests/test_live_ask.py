#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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
"""A Claude Code question answered from the workspace: the hook posts it, the bridge files the reply, the hook answers."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from one_conv.live import ask, bridge, claude, install, router
from one_conv.live.model import LiveSession

FRAMEWORK = {"question": "Which framework?", "header": "Framework", "multiSelect": False,
             "options": [{"label": "React", "description": "Most used"}, {"label": "Vue"}]}
EXTRAS = {"question": "Which extras?", "header": "Extras", "multiSelect": True,
          "options": [{"label": "Router"}, {"label": "Store"}]}


def payload(*questions, session="s1"):
    return {"session_id": session, "tool_name": "AskUserQuestion", "tool_input": {"questions": list(questions)}}


@pytest.fixture
def posted(monkeypatch):
    """The workspace post: records the text, and succeeds."""
    texts = []
    monkeypatch.setattr(ask, "_post", lambda session_id, text, reply="expected": texts.append(text) or True)
    return texts


@pytest.fixture
def answered(monkeypatch):
    """The human answers as soon as the question is posted; returns what the hook posted."""
    texts = []

    def reply(text):
        def post(session_id, posted_text, reply="expected"):
            texts.append(posted_text)
            ask.file_answer(session_id, "workspace/user", text)
            return True
        monkeypatch.setattr(ask, "_post", post)
        return texts
    return reply


# --- the question the human reads --------------------------------------------------------------


def test_the_question_shows_its_text():
    assert "**Framework**: Which framework?" in ask.question_text([FRAMEWORK])


def test_the_question_lists_each_option_with_its_description():
    assert "- React: Most used" in ask.question_text([FRAMEWORK])


def test_the_question_ends_with_the_marker_for_the_buttons():
    assert json.loads(ask.question_text([FRAMEWORK]).splitlines()[-1][len(ask.MARKER):-4])["questions"][0]["options"] == [
        {"label": "React", "description": "Most used"}, {"label": "Vue", "description": ""}]


# --- reading the reply ---------------------------------------------------------------------------


def test_a_reply_naming_an_option_picks_it_whatever_its_case():
    assert ask.answers([FRAMEWORK], "react") == {"Which framework?": "React"}


def test_any_other_reply_is_a_free_answer():
    assert ask.answers([FRAMEWORK], "Svelte, please") == {"Which framework?": "Svelte, please"}


def test_a_multi_select_reply_picks_each_named_option():
    assert ask.answers([EXTRAS], "store, router") == {"Which extras?": "Store, Router"}


def test_several_questions_are_answered_by_header_lines():
    assert ask.answers([FRAMEWORK, EXTRAS], "Framework: Vue\nExtras: Router") == {
        "Which framework?": "Vue", "Which extras?": "Router"}


# --- the bridge's side ------------------------------------------------------------------------------


def test_a_human_message_answers_a_pending_question(posted):
    (ask.asks_dir()).mkdir(parents=True)
    (ask.asks_dir() / "s1.json").write_text(json.dumps({"pid": os.getpid()}))
    assert ask.file_answer("s1", "workspace/user", "React")


@pytest.fixture
def abandoned():
    """A pending question whose hook was killed before it could clean up."""
    ask.asks_dir().mkdir(parents=True)
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    (ask.asks_dir() / "s1.json").write_text(json.dumps({"pid": int(finished.stdout)}))


def test_a_question_whose_hook_is_gone_takes_no_answer(abandoned):
    assert not ask.file_answer("s1", "workspace/user", "React")


def test_a_question_whose_hook_is_gone_is_cleared(abandoned):
    ask.file_answer("s1", "workspace/user", "React")
    assert not (ask.asks_dir() / "s1.json").exists()


def test_a_message_to_a_session_whose_hook_is_gone_reaches_its_terminal(abandoned, monkeypatch):
    delivered = []
    monkeypatch.setattr(router, "list_all", lambda harness=None: [LiveSession("claude", "s1")])
    monkeypatch.setattr(claude, "deliver", lambda session, text: delivered.append(text) or "delivered")
    bridge.deliver_inbound(human_reply())
    assert "React" in delivered[0]


def test_without_a_pending_question_a_message_is_not_an_answer():
    assert not ask.file_answer("s1", "workspace/user", "React")


def test_an_agent_message_never_answers_a_question():
    (ask.asks_dir()).mkdir(parents=True)
    (ask.asks_dir() / "s1.json").write_text(json.dumps({"pid": os.getpid()}))
    assert not ask.file_answer("s1", "macbook/claude:other", "React")


@pytest.fixture
def waiting_session(monkeypatch):
    """A live Claude Code session with a question pending; records what reaches its terminal."""
    delivered = []
    monkeypatch.setattr(router, "list_all", lambda harness=None: [LiveSession("claude", "s1")])
    monkeypatch.setattr(claude, "deliver", lambda session, text: delivered.append(text) or "delivered")
    ask.asks_dir().mkdir(parents=True)
    (ask.asks_dir() / "s1.json").write_text(json.dumps({"pid": os.getpid()}))
    return delivered


def human_reply(text="React"):
    return {"to": "claude:s1", "from": "workspace/user", "mode": "push", "reply": "expected", "text": text,
            "id": "a" * 32, "delivery": "b" * 32}


def test_the_bridge_reports_the_answer_accepted(waiting_session):
    assert bridge.deliver_inbound(human_reply())[0] == "accepted"


def test_the_bridge_keeps_the_answer_out_of_the_terminal(waiting_session):
    bridge.deliver_inbound(human_reply())
    assert waiting_session == []


def test_the_bridge_files_the_answer_for_the_hook(waiting_session):
    bridge.deliver_inbound(human_reply("Vue"))
    assert json.loads((ask.asks_dir() / "s1.answer.json").read_text())["text"] == "Vue"


# --- the hook ------------------------------------------------------------------------------------------


def test_the_hook_answers_the_tool_with_the_workspace_reply(answered):
    answered("Vue")
    assert ask.run_hook(payload(FRAMEWORK), wait=5)["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which framework?": "Vue"}


def test_the_hook_allows_the_tool_once_answered(answered):
    answered("Vue")
    assert ask.run_hook(payload(FRAMEWORK), wait=5)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_the_hook_echoes_the_questions_back(answered):
    answered("Vue")
    assert ask.run_hook(payload(FRAMEWORK), wait=5)["hookSpecificOutput"]["updatedInput"]["questions"] == [FRAMEWORK]


def test_the_hook_posts_the_question_to_the_workspace(posted):
    ask.run_hook(payload(FRAMEWORK), wait=0)
    assert "Which framework?" in posted[0]


def test_without_an_answer_in_time_the_terminal_asks(posted):
    assert ask.run_hook(payload(FRAMEWORK), wait=0) == {}


def test_without_an_answer_in_time_the_workspace_is_sent_to_the_terminal(posted):
    ask.run_hook(payload(FRAMEWORK), wait=0)
    assert "moved to my terminal" in posted[1]


def test_an_answered_question_sends_no_terminal_notice(answered):
    texts = answered("Vue")
    ask.run_hook(payload(FRAMEWORK), wait=5)
    assert len(texts) == 1


def test_the_terminal_notice_expects_no_reply(monkeypatch):
    replies = []
    monkeypatch.setattr(ask, "_post", lambda session_id, text, reply="expected": replies.append(reply) or True)
    ask.run_hook(payload(FRAMEWORK), wait=0)
    assert replies == ["expected", "none"]


def test_a_question_that_could_not_be_posted_is_left_to_the_terminal(monkeypatch):
    monkeypatch.setattr(ask, "_post", lambda session_id, text, reply="expected": False)
    assert ask.run_hook(payload(FRAMEWORK), wait=5) == {}


def test_the_hook_leaves_no_pending_question_behind(posted):
    ask.run_hook(payload(FRAMEWORK), wait=0)
    assert not (ask.asks_dir() / "s1.json").exists()


def test_the_hook_ignores_other_tools(posted):
    ask.run_hook({**payload(FRAMEWORK), "tool_name": "Bash"}, wait=0)
    assert posted == []


def test_a_question_already_answered_is_left_alone(posted):
    ask.run_hook({**payload(FRAMEWORK), "tool_input": {"questions": [FRAMEWORK], "answers": {"x": "y"}}}, wait=0)
    assert posted == []


def test_an_answer_arriving_while_the_hook_waits_is_used(posted, monkeypatch):
    monkeypatch.setattr(ask, "POLL_SECONDS", 0.05)
    timer = threading.Timer(0.3, lambda: ask.file_answer("s1", "workspace/user", "React"))
    timer.start()
    result = ask.run_hook(payload(FRAMEWORK), wait=5)
    timer.join()
    assert result["hookSpecificOutput"]["updatedInput"]["answers"] == {"Which framework?": "React"}


HOOK = """
import json, sys
from one_conv.live import ask
ask._post = lambda session_id, text, reply="expected": True
print(json.dumps(ask.run_hook(json.loads(sys.argv[1]), wait=60)))
"""


@pytest.fixture
def interrupted():
    """A real hook process waiting on its question, then ended the way Claude Code ends it on an interrupt."""
    hook = subprocess.Popen([sys.executable, "-c", HOOK, json.dumps(payload(FRAMEWORK))],
                            cwd=Path(__file__).resolve().parents[1], env=os.environ.copy())
    deadline = time.monotonic() + 30
    while not (ask.asks_dir() / "s1.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    hook.send_signal(signal.SIGTERM)
    hook.wait(timeout=30)
    return hook


def test_an_interrupted_hook_leaves_no_pending_question(interrupted):
    assert not (ask.asks_dir() / "s1.json").exists()


# --- install -------------------------------------------------------------------------------------------


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir()
    return tmp_path / "claude"


def ask_step():
    return next(step for step in install.plan(["one-conv"]) if "AskUserQuestion" in step.summary)


def test_install_registers_the_ask_hook(claude_home):
    ask_step().apply()
    assert json.loads((claude_home / "settings.json").read_text())["hooks"]["PreToolUse"] == [
        {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": "one-conv live ask-hook", "timeout": 1860}]}]


def test_install_ask_hook_is_done_after_apply(claude_home):
    ask_step().apply()
    assert ask_step().done()


def test_install_ask_hook_replaces_one_from_another_checkout(claude_home):
    (claude_home / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": "/old/one-conv live ask-hook"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}}))
    ask_step().apply()
    assert [entry["matcher"] for entry in json.loads((claude_home / "settings.json").read_text())["hooks"]["PreToolUse"]] == [
        "Bash", "AskUserQuestion"]
