"""Messages to `user`: notes for the person, filed on this machine or the relay and read with `live inbox`."""

import json

import pytest
from click.testing import CliRunner

from one_conv.live import router
from one_conv.live.model import LiveSession
from one_conv_cli import cli


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    """A live home with no relay configured, so notes stay on this machine."""
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path))
    monkeypatch.setenv("ONE_CONV_HUB_URL", "")
    monkeypatch.setattr(router, "_hub", lambda: None)
    return tmp_path


SENDER = LiveSession("claude", "11111111-2222-3333-4444-555555555555", title="release work")


def test_a_message_to_user_is_stored_on_this_machine(live_home):
    assert router.send("user", "deploy done", SENDER)["status"] == "stored on this machine"


def test_a_note_keeps_its_sender_name_and_tags(live_home):
    router.send("user", "deploy done", SENDER, tags=["deploy"])
    note = router.user_notes()["messages"][0]
    assert (note["from"], note["name"], note["tags"]) == (SENDER.address, "release work", ["deploy"])


def test_notes_are_numbered_in_order(live_home):
    router.send("user", "one", SENDER)
    router.send("user", "two", SENDER)
    assert [n["seq"] for n in router.user_notes()["messages"]] == [1, 2]


def test_a_terminal_sender_files_as_user(live_home):
    router.send("user", "remember the invoice", None, origin="user")
    assert router.user_notes()["messages"][0]["from"] == "user"


def test_a_tag_filter_keeps_matching_notes(live_home):
    router.send("user", "deploy done", SENDER, tags=["deploy"])
    router.send("user", "an idea", SENDER, tags=["ideas"])
    assert [n["text"] for n in router.user_notes(tags=["ideas"])["messages"]] == ["an idea"]


def test_latest_notes_keeps_the_newest(live_home):
    for text in ("one", "two", "three"):
        router.send("user", text, SENDER)
    assert [n["text"] for n in router.latest_notes(limit=2)["messages"]] == ["two", "three"]


@pytest.mark.parametrize("kwargs", [{"fyi": True}, {"images": ["shot.png"]}], ids=["fyi", "image"])
def test_a_note_carries_text_only(live_home, kwargs):
    with pytest.raises(router.LiveError):
        router.send("user", "hello", SENDER, **kwargs)


def test_an_upper_case_tag_is_refused(live_home):
    with pytest.raises(router.LiveError):
        router.send("user", "hello", SENDER, tags=["Deploy"])


def test_tags_are_refused_for_an_agent(live_home):
    with pytest.raises(router.LiveError, match="to user only"):
        router.send("claude:abc", "hello", SENDER, tags=["deploy"])


def test_inbox_shows_unread_notes(live_home):
    router.send("user", "deploy done", SENDER)
    result = CliRunner().invoke(cli, ["live", "inbox", "--json"])
    assert [n["text"] for n in json.loads(result.output)] == ["deploy done"]


def test_inbox_marks_what_it_showed_read(live_home):
    router.send("user", "deploy done", SENDER)
    CliRunner().invoke(cli, ["live", "inbox"])
    result = CliRunner().invoke(cli, ["live", "inbox", "--json"])
    assert json.loads(result.output) == []


def test_peek_leaves_notes_unread(live_home):
    router.send("user", "deploy done", SENDER)
    CliRunner().invoke(cli, ["live", "inbox", "--peek"])
    result = CliRunner().invoke(cli, ["live", "inbox", "--json"])
    assert len(json.loads(result.output)) == 1


def test_reading_one_tag_leaves_other_notes_unread(live_home):
    router.send("user", "deploy done", SENDER, tags=["deploy"])
    router.send("user", "an idea", SENDER, tags=["ideas"])
    CliRunner().invoke(cli, ["live", "inbox", "--tag", "deploy"])
    result = CliRunner().invoke(cli, ["live", "inbox", "--json"])
    assert [n["text"] for n in json.loads(result.output)] == ["deploy done", "an idea"]


def test_all_shows_read_notes_again(live_home):
    router.send("user", "deploy done", SENDER)
    CliRunner().invoke(cli, ["live", "inbox"])
    result = CliRunner().invoke(cli, ["live", "inbox", "--all", "--json"])
    assert [n["text"] for n in json.loads(result.output)] == ["deploy done"]


def test_send_to_user_is_a_dry_run_without_yes(live_home):
    CliRunner().invoke(cli, ["live", "send", "user", "hello"])
    assert router.user_notes()["messages"] == []
