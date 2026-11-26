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
"""A share that grants the transcript: read and search reach the shared session's archived copy and nothing else."""

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest

pytest.importorskip("uvicorn")

from one_conv.archive import ArchiveClient, pack
from one_conv.live import share_transcript
from one_conv.live.hub import Hub, application
from relay_worker import BACKENDS
from test_live_share import call, live, serve

ARCHIVED_AT = 1767225600.0
SHARED_TURNS = [
    {"ts": "2026-01-01T10:00:00Z", "role": "user",
     "blocks": [{"type": "text", "text": "please deploy the quartz service<system-reminder>hidden reminder</system-reminder>"}]},
    {"ts": "2026-01-01T10:01:00Z", "role": "assistant",
     "blocks": [{"type": "thinking", "thinking": "private quartz musing"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "deploy quartz"}},
                {"type": "tool_result", "content": "deploy log " + "x" * 9000},
                {"type": "text", "text": "deployed quartz"}]},
]
OTHER_TURNS = [{"ts": "2026-01-01T11:00:00Z", "role": "user", "blocks": [{"type": "text", "text": "the other quartz session"}]}]


def capture(session: str, turns: list) -> dict:
    """A Claude capture by node `mac`, shaped as the bridge's archive sync uploads it."""
    texts = [block["text"] for turn in turns for block in turn["blocks"] if block["type"] == "text"]
    return {"origin": "mac", "source": "claude", "cwd": "/srv/quartz", "session": session,
            "locator": f"/srv/claude/projects/-srv-quartz/{session}.jsonl", "mtime": ARCHIVED_AT,
            "capture_version": "1", "account_label": None, "unread_native": None,
            "summary": {"uuid": session, "source": "claude", "cwd": "/srv/quartz", "title": "Quartz", "turns": len(turns)},
            "turns": turns, "turns_with_subagents": turns, "search_text": texts,
            "originals": {f"/srv/claude/projects/-srv-quartz/{session}.jsonl": b"{}\n"}}


@pytest.fixture(params=BACKENDS)
def relay(request, tmp_path):
    """(state, url) of a relay with an archive: the Python relay with archive_db, or the Worker."""
    if request.param == "worker":
        state = request.getfixturevalue("relay_worker").hub()
        yield state, state.url
        return
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve(application(state, str(tmp_path / "archive.db")))
    yield state, url
    stop()


def make_share(url, mac, **extra):
    return call(url, "/v1/shares", mac, "POST", {"address": "claude:aaa", "label": "Alice", **extra})[2]


@pytest.fixture
def archived(relay):
    """Node `mac` publishes claude:aaa and claude:bbb, and its archive holds both sessions."""
    state, url = relay
    mac = state.add_token("mac")
    call(url, "/v1/sessions", mac, "PUT", {"sessions": [{"address": "claude:aaa", "name": "lead", "title": "Fix the build"},
                                                         {"address": "claude:bbb", "title": "Other"}]})
    client = ArchiveClient(url + "/archive", mac)
    client.put(*pack(capture("aaa", SHARED_TURNS)), None)
    client.put(*pack(capture("bbb", OTHER_TURNS)), None)
    return SimpleNamespace(state=state, url=url, mac=mac)


@pytest.fixture
def granted(archived):
    return SimpleNamespace(**vars(archived), share=make_share(archived.url, archived.mac, transcript=True))


@pytest.fixture
def messaging_only(archived):
    return SimpleNamespace(**vars(archived), share=make_share(archived.url, archived.mac))


def rpc(shared, method, params=None):
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return call(shared.url, f"/s/{shared.share['id']}/mcp", method="POST", body=message,
                headers={"Authorization": f"Bearer {shared.share['secret']}"})[2]


def tool(shared, name, **arguments):
    result = rpc(shared, "tools/call", {"name": name, "arguments": arguments})["result"]
    return result["content"][0]["text"] if result.get("isError") else result["structuredContent"]


def texts(page):
    return [turn["text"] for turn in page["turns"]]


def transcript_get(shared, path=""):
    return call(shared.url, f"/v1/shares/{shared.share['id']}/transcript{path}",
                headers={"X-Share-Secret": shared.share["secret"]})


# --- the grant ------------------------------------------------------------------------------------

def test_a_share_grants_no_transcript_by_default(messaging_only):
    assert messaging_only.share["transcript"] is False


def test_a_transcript_share_says_so(granted):
    assert granted.share["transcript"] is True


def test_the_listing_shows_the_transcript_grant(granted):
    assert call(granted.url, "/v1/shares", granted.mac)[2]["shares"][0]["transcript"] is True


def test_the_inbox_note_says_with_transcript(granted):
    assert "with transcript" in call(granted.url, "/v1/user?tag=share", granted.mac)[2]["messages"][0]["text"]


def test_a_non_boolean_transcript_flag_is_refused(archived):
    assert call(archived.url, "/v1/shares", archived.mac, "POST", {"address": "claude:aaa", "transcript": "yes"})[0] == 400


def test_a_relay_without_an_archive_refuses_a_transcript_grant(tmp_path):
    state = Hub(str(tmp_path / "hub.db"))
    url, stop = serve(application(state))
    mac = state.add_token("mac")
    call(url, "/v1/sessions", mac, "PUT", {"sessions": [{"address": "claude:aaa"}]})
    try:
        assert call(url, "/v1/shares", mac, "POST", {"address": "claude:aaa", "transcript": True})[0] == 409
    finally:
        stop()


# --- without the grant ----------------------------------------------------------------------------

def test_without_the_grant_the_transcript_tools_are_not_listed(messaging_only):
    names = {t["name"] for t in rpc(messaging_only, "tools/list")["result"]["tools"]}
    assert names & {"read_session", "search_session"} == set()


def test_without_the_grant_read_session_is_refused(messaging_only):
    assert "does not grant" in tool(messaging_only, "read_session")


def test_without_the_grant_search_session_is_refused(messaging_only):
    assert "does not grant" in tool(messaging_only, "search_session", query="quartz")


def test_without_the_grant_the_transcript_endpoint_answers_403(messaging_only):
    assert transcript_get(messaging_only)[0] == 403


def test_without_the_grant_the_page_shows_no_title(messaging_only):
    view = call(messaging_only.url, f"/v1/shares/{messaging_only.share['id']}/messages",
                headers={"X-Share-Secret": messaging_only.share["secret"]})[2]
    assert view["share"]["title"] == ""


# --- with the grant -------------------------------------------------------------------------------

def test_with_the_grant_the_transcript_tools_are_listed(granted):
    names = {t["name"] for t in rpc(granted, "tools/list")["result"]["tools"]}
    assert {"read_session", "search_session"} <= names


def test_read_session_returns_only_the_shared_sessions_visible_turns(granted):
    assert texts(tool(granted, "read_session")) == ["please deploy the quartz service", "deployed quartz"]


def test_read_session_ignores_a_key_for_another_session(granted):
    assert "the other quartz session" not in json.dumps(tool(granted, "read_session", key="bbb", session="bbb"))


def test_read_session_hides_thinking_without_raw(granted):
    assert "private quartz musing" not in json.dumps(tool(granted, "read_session"))


def test_read_session_with_raw_shows_the_tool_call(granted):
    assert '→ Bash({"command":"deploy quartz"})' in tool(granted, "read_session", raw=True)["turns"][1]["text"]


def test_read_session_with_raw_still_strips_system_reminders(granted):
    assert "hidden reminder" not in json.dumps(tool(granted, "read_session", raw=True))


def test_read_session_with_raw_still_cuts_long_tool_output(granted):
    assert "characters cut" in tool(granted, "read_session", raw=True)["turns"][1]["text"]


def test_read_session_pages_by_offset(granted):
    assert texts(tool(granted, "read_session", offset=1, limit=1)) == ["deployed quartz"]


def test_read_session_reports_the_archived_time(granted):
    assert tool(granted, "read_session")["archived_through"] == ARCHIVED_AT


def test_search_session_finds_only_the_shared_sessions_turns(granted):
    assert [hit["index"] for hit in tool(granted, "search_session", query="quartz")["hits"]] == [1, 0]


def test_search_session_never_searches_thinking(granted):
    assert tool(granted, "search_session", query="musing")["total_hits"] == 0


def test_search_session_needs_a_query(granted):
    assert "query" in tool(granted, "search_session", query="  ")


def test_session_info_with_the_grant_shows_the_title(granted):
    assert tool(granted, "session_info")["title"] == "Fix the build"


def test_session_info_with_the_grant_shows_when_the_copy_was_archived(granted):
    assert tool(granted, "session_info")["archived_through"] == ARCHIVED_AT


def test_the_transcript_endpoint_reads_the_shared_session(granted):
    assert texts(transcript_get(granted)[2]) == ["please deploy the quartz service", "deployed quartz"]


def test_the_transcript_search_endpoint_finds_turns(granted):
    assert transcript_get(granted, f"/search?query={quote('deployed quartz')}")[2]["total_hits"] == 1


def test_the_transcript_endpoint_refuses_a_wrong_secret(granted):
    status, _, _ = call(granted.url, f"/v1/shares/{granted.share['id']}/transcript", headers={"X-Share-Secret": "wrong"})
    assert status == 404


@pytest.mark.parametrize("query", ["?offset=abc", "?limit=0", "?limit=501"])
def test_the_transcript_endpoint_refuses_bad_paging(granted, query):
    assert transcript_get(granted, query)[0] == 400


def test_the_page_has_a_transcript_tab(granted):
    assert 'id="tab-transcript"' in call(granted.url, f"/s/{granted.share['id']}")[2]


# --- rendering, against the Worker's transcript.ts ----------------------------------------------------

@pytest.mark.parametrize(("turn", "raw", "text"), [
    ({"ts": "", "role": "user", "text": "plain <task-notification>x</task-notification>text"}, False, "plain text"),
    ({"ts": "", "role": "user", "blocks": [{"type": "tool_use", "name": "Read", "input": {}}]}, False, None),
    ({"ts": "", "role": "user", "blocks": [{"type": "tool_use", "name": "Read", "input": {}}]}, True, "→ Read({})"),
    ({"ts": "", "role": "user", "blocks": [{"type": "thinking", "thinking": " hm "}]}, True, "[thinking] hm"),
    ({"ts": "", "role": "user", "blocks": [{"type": "text", "text": "<command-message>m</command-message> <command-name>/x</command-name>"}]},
     False, "/x"),
    ({"ts": "", "role": "user", "blocks": [{"type": "tool_result", "content": [{"text": "a"}, {"text": ""}, {"text": "b"}]}]},
     True, "← a\nb"),
])
def test_a_turn_renders_as_the_remote_mcp_renders_it(turn, raw, text):
    assert share_transcript.render_turn(turn, raw) == text


# --- the owner's side -----------------------------------------------------------------------------

@pytest.fixture
def owner(archived, monkeypatch):
    """`live share` on node mac, with its relay configured through the environment."""
    monkeypatch.setenv("ONE_CONV_HUB_URL", archived.url)
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", archived.mac)
    monkeypatch.setenv("ONE_CONV_HUB_NODE", "mac")
    return archived


def test_live_share_transcript_grants_it(owner):
    made = json.loads(live("share", "--session", "claude:aaa", "--transcript", "--yes", "--json").output)
    assert made["transcript"] is True


def test_live_share_list_shows_with_transcript(owner):
    live("share", "--session", "claude:aaa", "--transcript", "--yes", "--json")
    assert "with transcript" in live("share", "--list").output


def test_live_share_dry_run_says_what_it_grants():
    assert "messages and the transcript" in live("share", "--session", "claude:aaa", "--transcript").output
