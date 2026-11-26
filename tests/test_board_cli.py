"""`one-conv board` and the board MCP tools against a real relay: read positions, dry runs, files, refusals."""

import asyncio
import hashlib
import json

import pytest
from click.testing import CliRunner
from mcp import Client

from one_conv import board, board_cli, mcp_server
from one_conv.live import router
from one_conv.live.model import LiveSession
from one_conv_cli import cli
from test_live_hub import call, hub  # noqa: F401  (fixture)

pytest.importorskip("uvicorn")

SESSION = LiveSession("claude", "aaa", title="release work")


@pytest.fixture
def relay(hub, monkeypatch):
    """This machine is node `mac` on a relay where it published claude:aaa; the CLI caller is the person."""
    state, url = hub
    token = state.add_token("mac")
    call(url, "/v1/sessions", token, "PUT", {"sessions": [{"address": "claude:aaa", "name": "lead"}]})
    monkeypatch.setenv("ONE_CONV_HUB_URL", url)
    monkeypatch.setenv("ONE_CONV_HUB_TOKEN", token)
    monkeypatch.setenv("ONE_CONV_HUB_NODE", "mac")
    monkeypatch.setattr(board_cli, "_session", lambda: None)
    monkeypatch.setattr(router, "origin", lambda interactive, pid=None: "user")
    monkeypatch.setattr(router, "whoami", lambda explicit=None, meta=None, pid=None: SESSION)
    return url, token


@pytest.fixture
def no_relay(monkeypatch):
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN", "ONE_CONV_HUB_NODE"):
        monkeypatch.delenv(name, raising=False)


def run(*args, stdin=None):
    return CliRunner().invoke(cli, ["board", *args], input=stdin)


def posted(*args):
    """Post as the person and return the receipt."""
    return json.loads(run("post", *args, "--yes", "--json").output)


def read_json(*args):
    return json.loads(run("read", *args, "--json").output)


def test_without_a_relay_the_board_refuses(no_relay):
    assert "no relay configured" in run("topics").output


def test_without_a_relay_a_dry_run_refuses_too(no_relay):
    assert run("post", "release", "hello").exit_code == 1


def test_a_dry_run_posts_nothing(relay):
    run("post", "release", "hello")
    assert run("topics", "--json").output.strip() == "[]"


def test_a_dry_run_names_the_author(relay):
    assert "from:     user" in run("post", "release", "hello").output


def test_a_post_reads_back(relay):
    posted("release", "ship monday")
    assert [p["text"] for p in read_json("release", "--as", "box-reader")["posts"]] == ["ship monday"]


def test_the_person_posts_as_user(relay):
    posted("release", "ship monday")
    assert read_json("release", "--as", "other")["posts"][0]["from"] == "mac/user"


def test_reading_marks_posts_read(relay):
    posted("release", "one")
    run("read", "release", "--as", "r")
    assert read_json("release", "--as", "r")["posts"] == []


def test_peek_leaves_posts_unread(relay):
    posted("release", "one")
    run("read", "release", "--as", "r", "--peek")
    assert len(read_json("release", "--as", "r")["posts"]) == 1


def test_all_shows_read_posts_again(relay):
    posted("release", "one")
    run("read", "release", "--as", "r")
    assert [p["text"] for p in read_json("release", "--as", "r", "--all")["posts"]] == ["one"]


def test_all_keeps_the_newest(relay):
    for text in ("one", "two", "three"):
        posted("release", text)
    assert [p["text"] for p in read_json("release", "--all", "--limit", "2")["posts"]] == ["two", "three"]


def test_reading_one_tag_leaves_other_posts_unread(relay):
    posted("release", "a decision", "--tag", "decision")
    posted("release", "a finding", "--tag", "finding")
    run("read", "release", "--as", "r", "--tag", "decision")
    assert [p["text"] for p in read_json("release", "--as", "r")["posts"]] == ["a decision", "a finding"]


def test_readers_keep_their_own_positions(relay):
    posted("release", "one")
    run("read", "release", "--as", "first")
    assert len(read_json("release", "--as", "second")["posts"]) == 1


def test_topics_count_unread_posts_per_reader(relay):
    posted("release", "one")
    posted("release", "two")
    assert json.loads(run("topics", "--as", "r", "--json").output)[0]["unread"] == 2


def test_the_posters_own_post_is_not_unread_for_it(relay):
    posted("release", "one")
    assert json.loads(run("topics", "--json").output)[0]["unread"] == 0


def test_reading_an_unknown_topic_says_so(relay):
    assert "no topic named nowhere" in run("read", "nowhere").output


def test_a_malformed_topic_is_refused_before_posting(relay):
    assert "A topic is" in run("post", "Release", "hi", "--yes").output


def test_text_over_32kb_is_refused_before_posting(relay):
    assert "at most 32768 bytes" in run("post", "release", "x" * 40_000, "--yes").output


def test_a_post_reads_its_text_from_stdin(relay):
    run("post", "release", "--stdin", "--yes", stdin="from stdin\n")
    assert read_json("release", "--as", "r")["posts"][0]["text"] == "from stdin\n"


def test_reposting_the_same_note_is_stored_once(relay):
    posted("release", "once")
    assert posted("release", "once")["duplicate"] is True


@pytest.fixture
def attached(relay, tmp_path):
    """A post on `release` carrying data.bin; returns its file id and bytes."""
    data = bytes(range(256)) * 40
    (tmp_path / "data.bin").write_bytes(data)
    posted("release", "the data", "--file", str(tmp_path / "data.bin"))
    return read_json("release", "--as", "r", "--peek")["posts"][0]["files"][0]["id"], data


def test_a_file_downloads_byte_identical(attached, tmp_path):
    file_id, data = attached
    run("get", "release", file_id, "-o", str(tmp_path / "copy.bin"))
    assert (tmp_path / "copy.bin").read_bytes() == data


def test_a_download_reports_the_sha256(attached, tmp_path):
    file_id, data = attached
    result = json.loads(run("get", "release", file_id, "-o", str(tmp_path / "copy.bin"), "--json").output)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()


def test_a_download_never_overwrites_without_force(attached, tmp_path):
    file_id, _ = attached
    (tmp_path / "copy.bin").write_text("mine")
    run("get", "release", file_id, "-o", str(tmp_path / "copy.bin"))
    assert (tmp_path / "copy.bin").read_text() == "mine"


def test_a_missing_file_is_refused_before_posting(relay, tmp_path):
    assert "is not a file" in run("post", "release", "x", "--file", str(tmp_path / "nope"), "--yes").output


def test_five_files_are_refused_before_posting(relay, tmp_path):
    paths = []
    for index in range(5):
        (tmp_path / f"f{index}").write_text("x")
        paths += ["--file", str(tmp_path / f"f{index}")]
    assert "at most 4 files" in run("post", "release", "x", *paths, "--yes").output


def test_search_finds_a_post(relay):
    posted("release", "rollback is a config flip")
    assert [p["text"] for p in json.loads(run("search", "rollback", "--json").output)] == ["rollback is a config flip"]


def test_rm_without_yes_deletes_nothing(relay):
    receipt = posted("release", "gone?")
    run("rm", "release", receipt["id"])
    assert len(read_json("release", "--as", "r")["posts"]) == 1


def test_rm_hides_the_post(relay):
    receipt = posted("release", "gone")
    run("rm", "release", receipt["id"], "--yes")
    assert read_json("release", "--as", "r")["posts"] == []


def test_the_feed_line_names_new_posts_in_a_known_topic(relay):
    posted("release", "one")
    run("read", "release", "--as", "r")
    posted("release", "two")
    assert board.unread_lines("r") == ["board: 1 new post in release"]


def test_local_feed_shows_the_board_line(relay):
    posted("release", "one")
    run("read", "release", "--as", "claude:aaa")
    posted("release", "two")
    result = CliRunner().invoke(cli, ["local", "feed", "--as", "claude:aaa", "--json"])
    assert json.loads(result.output)["board"] == ["board: 1 new post in release"]


def test_the_feed_is_silent_for_topics_the_reader_never_touched(relay):
    posted("release", "one")
    assert board.unread_lines("stranger") == []


def call_tool(tool, arguments):
    """(is_error, structured result or error text) of one in-process MCP tool call."""
    async def go():
        async with Client(mcp_server.create_server()) as client:
            result = await client.call_tool(tool, arguments)
            if result.is_error:
                return True, " ".join(getattr(item, "text", "") for item in result.content)
            return False, result.structured_content
    return asyncio.run(go())


def test_board_post_tool_posts_as_the_calling_session(relay):
    call_tool("board_post", {"topic": "release", "text": "from an agent"})
    assert read_json("release", "--as", "r")["posts"][0]["from"] == "mac/claude:aaa"


def test_board_post_tool_attaches_a_local_file(relay, tmp_path):
    (tmp_path / "notes.md").write_text("# notes")
    call_tool("board_post", {"topic": "release", "text": "notes", "files": [str(tmp_path / "notes.md")]})
    assert read_json("release", "--as", "r")["posts"][0]["files"][0]["name"] == "notes.md"


def test_board_read_tool_marks_posts_read_for_the_session(relay):
    posted("release", "one")
    call_tool("board_read", {"topic": "release"})
    assert call_tool("board_read", {"topic": "release"})[1]["posts"] == []


def test_board_read_tool_peek_leaves_posts_unread(relay):
    posted("release", "one")
    call_tool("board_read", {"topic": "release", "peek": True})
    assert len(call_tool("board_read", {"topic": "release"})[1]["posts"]) == 1


def test_board_topics_tool_lists_unread_counts(relay):
    posted("release", "one")
    assert call_tool("board_topics", {})[1]["topics"][0]["unread"] == 1


def test_board_search_tool_finds_a_post(relay):
    posted("release", "rollback is a config flip")
    assert len(call_tool("board_search", {"query": "rollback"})[1]["posts"]) == 1


def test_a_board_tool_refusal_tells_the_agent_why(relay):
    assert "A topic is" in call_tool("board_post", {"topic": "Bad Topic", "text": "x"})[1]
