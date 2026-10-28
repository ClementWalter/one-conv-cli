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
"""A terminal session's conversation reaches the workspace: typed prompts and text replies, filtered, dated, once, no wake."""

import json
import os
import sys
from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from one_conv import feed
from one_conv.control.store import ControlStore
from one_conv.live import bridge, prompts
from one_conv.live.hub import Hub, application
from tests.test_live_hub import serve

SESSION = "claude:c1"
NODE = "macbook"


@dataclass
class StubTurn:
    role: str
    text: str
    ts: str = "2026-10-20T10:00:00Z"
    blocks: list = field(default_factory=list)

    def __post_init__(self):
        if not self.blocks and self.text:
            self.blocks = [{"type": "text", "text": self.text}]


def thread(turns, stamp="s1"):
    return feed.Thread(address=SESSION, title="work", cwd="/work", stamp=stamp, load=lambda: turns,
                       visible=lambda rows: rows, render=lambda turn: turn.text or None)


def typed(text, ts="2026-10-20T10:00:00Z"):
    return StubTurn("user", text, ts)


def relayed():
    return StubTurn("user", '<one-conv-message from="workspace/user" kind="human">\nhey\n</one-conv-message>')


def tool_result():
    return StubTurn("user", "", blocks=[{"type": "tool_result", "content": "ok"}])


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    return tmp_path / "live"


class StubClient:
    """A hub that answers every reported id with one status."""

    def __init__(self, status="recorded"):
        self.status, self.batches = status, []

    def request(self, method, path, body=None):
        self.batches.append(body["messages"])
        return {"results": [{"id": item["id"], "status": self.status} for item in body["messages"]]}


# --- which turns are prompts ------------------------------------------------------------------


def test_a_relayed_envelope_is_not_a_prompt():
    assert prompts.conversation(thread([relayed()]), [relayed()], 0, False) == []


@pytest.mark.parametrize("text", [
    "<command-name>/model</command-name>\n<command-message>model</command-message>",
    "<command-message>commit</command-message>\n<command-name>/commit</command-name>",
    "<local-command-caveat>The command below was run directly in Claude Code.</local-command-caveat>",
    "<local-command-stdout>Kept model as Mythos</local-command-stdout>",
])
def test_slash_command_plumbing_is_not_a_prompt(text):
    turns = [StubTurn("user", text)]
    assert prompts.conversation(thread(turns), turns, 0, False) == []


def test_a_bare_image_attachment_turn_is_not_a_prompt():
    turns = [StubTurn("user", "[Image: source: /tmp/images/1.png]")]
    assert prompts.conversation(thread(turns), turns, 0, False) == []


def test_a_prompt_retyped_longer_before_any_answer_replaces_the_interrupted_one():
    turns = [typed("yes ok make sense", "2026-10-20T10:00:00Z"), typed("yes ok make sense, and deploy it", "2026-10-20T10:00:30Z")]
    assert [row["text"] for row in prompts.conversation(thread(turns), turns, 0, False)] == ["yes ok make sense, and deploy it"]


def test_a_prompt_repeated_after_an_answer_is_a_new_prompt():
    turns = [typed("yes"), StubTurn("assistant", "Done."), typed("yes")]
    assert [row["text"] for row in prompts.conversation(thread(turns), turns, 0, False)] == ["yes", "Done.", "yes"]


def test_a_tool_result_turn_is_not_a_prompt():
    turns = [tool_result()]
    assert prompts.conversation(thread(turns), turns, 0, False) == []


def test_an_assistant_text_turn_is_a_reply():
    turns = [StubTurn("assistant", "Done.")]
    assert [row["role"] for row in prompts.conversation(thread(turns), turns, 0, False)] == ["assistant"]


def test_an_assistant_turn_without_prose_is_left_out():
    turns = [StubTurn("assistant", "", blocks=[{"type": "tool_use", "name": "Bash", "input": {}}])]
    assert prompts.conversation(thread(turns), turns, 0, False) == []


def test_a_typed_prompt_keeps_its_index_and_time():
    turns = [StubTurn("assistant", "Hello"), typed("fix the sidebar", "2026-10-20T10:05:00Z")]
    assert prompts.conversation(thread(turns), turns, 0, False)[1] == {
        "index": 1, "ts": "2026-10-20T10:05:00Z", "role": "user", "text": "fix the sidebar"}


def test_turns_before_the_position_are_left_out():
    turns = [typed("old"), StubTurn("assistant", "ok"), typed("new")]
    assert [row["text"] for row in prompts.conversation(thread(turns), turns, 2, False)] == ["new"]


def test_first_sight_keeps_only_the_recent_prompts():
    turns = [typed(f"prompt {i}") for i in range(prompts.FIRST_SIGHT + 5)]
    assert len(prompts.conversation(thread(turns), turns, None, False)) == prompts.FIRST_SIGHT


def test_a_spawned_sessions_first_prompt_is_the_order_not_the_human():
    turns = [typed("review the migration"), StubTurn("assistant", "ok"), typed("thanks")]
    assert [row["text"] for row in prompts.conversation(thread(turns), turns, 0, True)] == ["ok", "thanks"]


def test_prompt_id_is_stable():
    assert prompts.prompt_id(NODE, SESSION, 3, "t") == prompts.prompt_id(NODE, SESSION, 3, "t")


def test_prompt_id_is_32_hex_characters():
    assert len(prompts.prompt_id(NODE, SESSION, 3, "t")) == 32 and int(prompts.prompt_id(NODE, SESSION, 3, "t"), 16) >= 0


def test_item_carries_the_turns_own_time():
    row = {"index": 0, "ts": "2026-10-20T10:00:00Z", "role": "user", "text": "hi"}
    assert prompts.item(NODE, SESSION, row)["at"] == 1_792_490_400.0


def test_a_prompt_is_from_the_human_to_the_session_and_expects_no_reply():
    row = prompts.item(NODE, SESSION, {"index": 0, "ts": "2026-10-20T10:00:00Z", "role": "user", "text": "hi"})
    assert (row["from"], row["to"], row["reply"], row["outcome"]) == ("user", SESSION, "none", "accepted")


def test_a_reply_is_from_the_session_to_the_human():
    row = prompts.item(NODE, SESSION, {"index": 1, "ts": "2026-10-20T10:00:01Z", "role": "assistant", "text": "Done."})
    assert (row["from"], row["to"]) == (SESSION, "user")


# --- reporting ------------------------------------------------------------------------------------


def test_report_sends_the_new_prompts_and_replies(home):
    client = StubClient()
    prompts.report(client, NODE, {SESSION: thread([typed("first"), StubTurn("assistant", "Done.")])}, {})
    assert [item["text"] for item in client.batches[0]] == ["first", "Done."]


def test_report_moves_the_position_once_the_hub_answered(home):
    turns = [typed("first"), StubTurn("assistant", "ok")]
    state = {}
    prompts.report(StubClient(), NODE, {SESSION: thread(turns)}, state)
    assert state[SESSION]["count"] == 2


def test_report_keeps_the_position_when_the_hub_did_not_answer(home):
    class Silent:
        def request(self, method, path, body=None):
            return {}
    state = {}
    prompts.report(Silent(), NODE, {SESSION: thread([typed("first")])}, state)
    assert SESSION not in state


def test_report_sends_nothing_twice(home):
    turns = [typed("first")]
    client, state = StubClient(), {}
    prompts.report(client, NODE, {SESSION: thread(turns)}, state)
    prompts.report(client, NODE, {SESSION: thread(turns)}, state)
    assert len(client.batches) == 1


def test_report_saves_its_positions_on_disk(home):
    prompts.report(StubClient(), NODE, {SESSION: thread([typed("first")])})
    assert SESSION in json.loads(prompts.cursor_path().read_text())


def test_spawned_sessions_are_read_from_the_registry(home):
    home.mkdir()
    (home / "spawned.json").write_text(json.dumps({"job": {"harness": "claude", "cwd": "/w", "address": SESSION}}))
    assert prompts.spawned_addresses() == {SESSION}


# --- hub ------------------------------------------------------------------------------------------


@pytest.fixture
def hub(tmp_path):
    return Hub(str(tmp_path / "hub.sqlite3"))


@pytest.fixture
def reported():
    return prompts.item(NODE, SESSION, {"index": 0, "ts": "2026-10-20T10:00:00Z", "role": "user", "text": "fix the sidebar"})


@pytest.fixture
def reply():
    return prompts.item(NODE, SESSION, {"index": 1, "ts": "2026-10-20T10:00:05Z", "role": "assistant", "text": "Fixed."})


def test_hub_records_a_terminal_prompt(hub, reported):
    assert hub.report(NODE, [reported])["results"][0]["status"] == "recorded"


def test_hub_records_a_terminal_reply(hub, reply):
    assert hub.report(NODE, [reply])["results"][0]["status"] == "recorded"


def test_hub_refuses_a_report_from_the_human_to_the_human(hub, reported):
    assert hub.report(NODE, [{**reported, "to": "user"}])["results"][0]["status"] == "refused"


def test_hub_refuses_a_report_to_the_workspace_human(hub, reply):
    assert hub.report(NODE, [{**reply, "to": "workspace/user"}])["results"][0]["status"] == "refused"


def test_a_terminal_prompt_wakes_nobody(hub, reported):
    hub.report(NODE, [reported])
    assert hub.inbox(NODE, 0) == []


# --- workspace --------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, reported, reply):
    store = ControlStore(tmp_path / "workspace")
    store.hub.add_token(NODE)
    store.hub.report(NODE, [reported, reply])
    return store


def test_workspace_lists_the_prompt_in_the_conversation_with_the_session(store):
    assert [t["pair"] for t in store.direct()["threads"]] == [f"{NODE}/{SESSION} user"]


def test_workspace_shows_the_prompt_as_you(store):
    assert store.direct()["threads"][0]["messages"][0]["from"] == "user"


def test_workspace_shows_the_reply_from_the_session_in_the_same_thread(store):
    assert store.direct()["threads"][0]["messages"][1]["from"] == f"{NODE}/{SESSION}"


def test_workspace_marks_mirrored_turns(store):
    assert [m["mirrored"] for m in store.direct()["threads"][0]["messages"]] == [True, True]


def test_a_mirrored_turn_carries_no_terminal_label(store):
    assert "terminal" not in json.dumps(store.direct()["threads"][0]["messages"]).lower()


def test_an_old_terminal_reply_is_not_unread(store):
    assert store.direct()["threads"][0]["unread"] == 0


# --- bridge runner ------------------------------------------------------------------------------------


@pytest.fixture
def recorder(tmp_path, home):
    """A stand-in launcher that records how it was called."""
    output = tmp_path / "called.json"
    script = tmp_path / "launcher.py"
    script.write_text("import json, os, sys\n"
                      f"open({str(output)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], "
                      "'env': {k: v for k, v in os.environ.items() if k.startswith('ONE_CONV_HUB')}}))\n")
    sync = bridge.PromptSync(seconds=5, launcher=script)
    sync.poll("https://hub.example.test", "node-token", NODE)
    sync.process.wait(timeout=30)
    return sync, json.loads(output.read_text())


def test_prompt_sync_runs_the_report_command(recorder):
    assert recorder[1]["argv"] == ["report-prompts"]


def test_prompt_sync_hands_the_hub_over_in_the_environment(recorder):
    assert recorder[1]["env"] == {"ONE_CONV_HUB_URL": "https://hub.example.test", "ONE_CONV_HUB_TOKEN": "node-token",
                                  "ONE_CONV_HUB_NODE": NODE}


def test_prompt_sync_waits_for_its_interval(recorder):
    assert recorder[0].poll("https://hub.example.test", "node-token", NODE) is False


def test_prompt_sync_turned_off_never_starts(tmp_path, home):
    assert bridge.PromptSync(seconds=0, launcher=tmp_path / "missing.py").poll("https://h", "t", NODE) is False


# --- the command against a real hub --------------------------------------------------------------------


@pytest.fixture
def served(tmp_path, home, monkeypatch):
    state = Hub(str(tmp_path / "hub.db"))
    token = state.add_token(NODE)
    url, stop = serve(application(state))
    monkeypatch.setenv("ONE_CONV_HUB_URL", url)
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", token)
    monkeypatch.setenv("ONE_CONV_HUB_NODE", NODE)
    yield state
    stop()


def test_report_prompts_command_reports_this_machines_sessions(served):
    turns = [typed("fix the sidebar"), relayed(), StubTurn("assistant", "Fixed.")]
    command = prompts.build_prompts_command(lambda addresses: ({SESSION: thread(turns)}, []), lambda: [SESSION])
    result = CliRunner().invoke(command, ["--json"])
    assert json.loads(result.output)["recorded"] == 2


def test_report_prompts_command_needs_a_hub(home, monkeypatch):
    for variable in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN", "ONE_CONV_HUB_NODE"):
        monkeypatch.delenv(variable, raising=False)
    command = prompts.build_prompts_command(lambda addresses: ({}, []), list)
    assert "live hub" in CliRunner().invoke(command, []).output


def test_launcher_knows_the_report_command():
    import subprocess
    result = subprocess.run([sys.executable, str(bridge.LAUNCHER), "report-prompts", "--help"],
                            capture_output=True, text=True, env={**os.environ}, check=False)
    assert "prompts typed into this machine" in result.stdout
