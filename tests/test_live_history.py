"""Same-machine agent messages reach the workspace as history only: original ids, times and outcomes, no wake."""

import json
import logging
import sqlite3

import pytest

from one_conv.control.store import ControlStore
from one_conv.live import bridge, history, hubclient, router, team
from one_conv.live.hub import MAX_TEXT, REPORT_BATCH, Hub, HubError, application
from tests.test_live_hub import serve

AT = 1_790_000_000.0


@pytest.fixture
def item():
    return {"id": "a" * 32, "from": "codex:c1", "to": "claude:c2", "topic": None, "mode": "push",
            "reply": "expected", "text": "Please rerun the suite.", "at": AT, "outcome": "accepted"}


@pytest.fixture
def hub(tmp_path):
    return Hub(str(tmp_path / "hub.sqlite3"))


# --- hub -------------------------------------------------------------------------------------------


def test_reported_message_is_recorded(hub, item):
    assert hub.report("mac", [item])["results"] == [{"id": "a" * 32, "status": "recorded"}]


def test_reported_message_never_reaches_an_inbox(hub, item):
    hub.report("mac", [item])
    assert hub.inbox("mac", 0) == []


def test_reported_message_keeps_its_outcome(hub, item):
    hub.report("mac", [{**item, "outcome": "unconfirmed"}])
    assert hub.status("mac", "a" * 32)["recipients"][0]["outcome"] == "unconfirmed"


def test_reported_message_keeps_its_original_time(hub, item):
    hub.report("mac", [item])
    with hub._db() as db:
        assert db.execute("SELECT created FROM posts").fetchone()[0] == AT


def test_a_retry_keeps_the_same_original_time(hub, item):
    hub.report("mac", [{**item, "at": 30_000_000_000.0}])
    assert hub.report("mac", [{**item, "at": 30_000_000_000.0}])["results"][0]["status"] == "duplicate"


@pytest.mark.parametrize("change", [{"at": AT + 1}, {"outcome": "unconfirmed"}])
def test_a_changed_observation_is_a_conflict(hub, item, change):
    hub.report("mac", [item])
    assert hub.report("mac", [{**item, **change}])["results"][0]["status"] == "conflict"


def test_a_conflict_keeps_the_first_receipt(hub, item):
    hub.report("mac", [item])
    hub.report("mac", [{**item, "outcome": "unconfirmed"}])
    assert hub.status("mac", "a" * 32)["recipients"][0]["outcome"] == "accepted"


@pytest.mark.parametrize("at", [True, float("nan"), float("inf"), 0, -5.0])
def test_an_invalid_time_is_refused(hub, item, at):
    assert hub.report("mac", [{**item, "at": at}])["results"][0]["status"] == "refused"


def test_reporting_creates_no_membership(hub, item):
    hub.report("mac", [{**item, "topic": "release"}])
    assert hub.topics()[0]["members"] == []


def test_history_names_a_tagged_dms_recipient(hub, item):
    hub.report("mac", [{**item, "topic": "release"}])
    assert hub.history("release")[0]["to"] == "mac/claude:c2"


def test_history_marks_reported_posts(hub, item):
    hub.report("mac", [{**item, "topic": "release"}])
    assert hub.history("release")[0]["reported"] is True


def test_reported_message_targets_the_reporting_nodes_agent(hub, item):
    hub.report("mac", [item])
    with hub._db() as db:
        assert db.execute("SELECT target FROM posts").fetchone()[0] == "mac/claude:c2"


def test_reported_message_is_marked_reported(hub, item):
    hub.report("mac", [item])
    with hub._db() as db:
        assert db.execute("SELECT reported FROM posts").fetchone()[0] == 1


def test_a_retry_is_a_duplicate(hub, item):
    hub.report("mac", [item])
    assert hub.report("mac", [item])["results"][0]["status"] == "duplicate"


def test_the_same_id_with_other_text_is_a_conflict(hub, item):
    hub.report("mac", [item])
    assert hub.report("mac", [{**item, "text": "Something else."}])["results"][0]["status"] == "conflict"


def test_a_conflict_keeps_the_first_text(hub, item):
    hub.report("mac", [item])
    hub.report("mac", [{**item, "text": "Something else."}])
    with hub._db() as db:
        assert db.execute("SELECT text FROM posts").fetchone()[0] == "Please rerun the suite."


@pytest.mark.parametrize("change", [
    {"from": "user"}, {"to": "user"}, {"to": "workspace/user"}, {"from": "box/codex:c9"}, {"to": "#release"},
    {"id": "not-hex"}, {"topic": "Bad Topic"}, {"text": ""}, {"outcome": "read"}, {"mode": "loud"}, {"at": "yesterday"},
])
def test_an_invalid_item_is_refused(hub, item, change):
    assert hub.report("mac", [{**item, **change}])["results"][0]["status"] == "refused"


def test_invalid_unicode_is_refused_without_blocking_the_rest(hub, item):
    results = hub.report("mac", [{**item, "text": "bad \ud800"}, {**item, "id": "b" * 32}])["results"]
    assert [row["status"] for row in results] == ["refused", "recorded"]


def test_an_invalid_item_does_not_block_the_rest(hub, item):
    results = hub.report("mac", [{**item, "from": "user"}, {**item, "id": "b" * 32}])["results"]
    assert [row["status"] for row in results] == ["refused", "recorded"]


@pytest.mark.parametrize("items", [[], "not a list"])
def test_an_empty_or_malformed_batch_is_refused(hub, items):
    with pytest.raises(HubError):
        hub.report("mac", items)


def test_an_oversized_batch_is_refused(hub, item):
    with pytest.raises(HubError):
        hub.report("mac", [{**item, "id": f"{i:032x}"} for i in range(REPORT_BATCH + 1)])


def test_an_existing_database_gains_the_reported_column(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE posts (seq INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, id TEXT NOT NULL,"
                   " digest TEXT NOT NULL, target TEXT NOT NULL, sender TEXT NOT NULL, topic TEXT, reply TEXT NOT NULL,"
                   " mode TEXT NOT NULL DEFAULT 'push', text TEXT NOT NULL, created REAL NOT NULL,"
                   " UNIQUE (from_node, id))")
    Hub(str(path))
    with sqlite3.connect(path) as db:
        assert "reported" in {row[1] for row in db.execute("PRAGMA table_info(posts)")}


# --- local log selection ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    (tmp_path / "live").mkdir()
    return tmp_path / "live"


@pytest.fixture
def row():
    return {"id": "a" * 32, "from": "codex:c1", "to": "claude:c2", "topic": None, "mode": "push", "reply": "expected",
            "status": "delivered", "text": "Please rerun the suite.", "at": AT}


def _log(home, *rows):
    (home / "messages.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_a_delivered_local_dm_is_eligible(row):
    assert history.eligible(row) is True


def test_a_stored_fyi_is_eligible(row):
    assert history.eligible({**row, "status": "stored FYI"}) is True


@pytest.mark.parametrize("change", [
    {"to": "macbook/codex:c3"}, {"to": "#release"}, {"to": "user"}, {"from": "user"},
    {"status": "stored on the hub"}, {"status": {"omp:o1": "delivered"}}, {"status": "stored for human"},
    {"status": ""}, {"id": "short"}, {"text": ""}, {"at": None}, {"at": True},
])
def test_rows_that_went_elsewhere_are_not_eligible(row, change):
    assert history.eligible({**row, **change}) is False


def test_a_queued_local_dm_is_eligible(row):
    assert history.eligible({**row, "status": "queued until the thread is resumed"}) is True


def test_a_queued_local_dm_is_reported_unconfirmed(row):
    assert history.item({**row, "status": "queued until the thread is resumed"})["outcome"] == "unconfirmed"


def test_reported_item_maps_the_local_status_to_acceptance(row):
    assert history.item(row)["outcome"] == "accepted"


def test_a_legacy_fyi_never_asks_for_a_reply(row):
    assert history.item({**row, "mode": "fyi", "reply": "expected"})["reply"] == "none"


def test_a_no_reply_push_stays_a_push(row):
    assert history.item({**row, "reply": "none"})["mode"] == "push"


def test_reported_item_keeps_the_topic_tag(row):
    assert history.item({**row, "topic": "release"})["topic"] == "release"


def test_a_corrupt_log_line_does_not_hide_the_rest(home, row):
    (home / "messages.jsonl").write_text("{not json\n" + json.dumps(row) + "\n")
    assert [r["id"] for r in history.pending()] == ["a" * 32]


# --- reporting through the real hub over HTTP ------------------------------------------------------------


@pytest.fixture
def served(home, tmp_path):
    store = ControlStore(tmp_path / "workspace")
    url, stop = serve(application(store.hub))
    client = hubclient.HubClient(url, store.hub.add_token("macbook"), "macbook")
    yield store, bridge.Bridge(client, "macbook", bridge.Ingress(tmp_path / "ingress.db"))
    stop()


def test_the_bridge_reports_a_local_dm(home, served, row):
    _log(home, row)
    assert served[1].report_history()["recorded"] == 1


def test_a_reported_agent_dm_stays_out_of_the_human_direct_threads(home, served, row):
    _log(home, row)
    served[1].report_history()
    assert served[0].direct()["threads"] == []


def test_reported_rows_are_not_sent_again(home, served, row):
    _log(home, row)
    served[1].report_history()
    assert history.pending() == []


def test_a_lost_answer_is_settled_as_a_duplicate(home, served, row):
    _log(home, row)
    served[1].report_history()
    history.cursor_path().unlink()
    assert served[1].report_history()["duplicate"] == 1


def test_one_batch_per_step(home, served, row):
    _log(home, *({**row, "id": f"{i:032x}"} for i in range(REPORT_BATCH + 5)))
    served[1].report_history()
    assert len(history.pending()) == 5


def test_the_most_recent_rows_are_reported_first(home, served, row):
    _log(home, *({**row, "id": f"{i:032x}"} for i in range(REPORT_BATCH + 5)))
    served[1].report_history()
    assert [r["id"] for r in history.pending()] == [f"{i:032x}" for i in range(5)]


def test_a_batch_respects_the_byte_budget(home, served, row, monkeypatch):
    monkeypatch.setattr(history, "BATCH_BYTES", 1000)
    _log(home, *({**row, "id": f"{i:032x}", "text": "x" * 300} for i in range(5)))
    served[1].report_history()
    assert len(history.pending()) == 3


def test_a_row_at_the_hub_size_limit_is_reported(home, served, row):
    _log(home, {**row, "text": "é" * MAX_TEXT})
    assert served[1].report_history()["recorded"] == 1


def test_a_row_too_large_for_any_batch_is_settled(home, served, row, monkeypatch):
    monkeypatch.setattr(history, "BATCH_BYTES", 1000)
    _log(home, {**row, "text": "x" * 2000})
    served[1].report_history()
    assert history.pending() == []


def test_reporting_wakes_no_agent(home, served, row):
    _log(home, row)
    served[1].report_history()
    assert served[0].hub.inbox("macbook", 0) == []


def test_a_conflict_is_logged_and_settled(home, served, row, caplog):
    _log(home, row)
    served[1].report_history()
    history.cursor_path().unlink()
    _log(home, {**row, "text": "Edited locally."})
    caplog.set_level(logging.WARNING)
    served[1].report_history()
    assert "kept no history for message" in caplog.text


def test_an_unreachable_history_route_does_not_stop_deliveries(home, served, row, monkeypatch):
    _log(home, row)

    def unreachable(method, path, body=None):
        if path == "/v1/history":
            raise hubclient.HubUnavailable("timed out")
        return {"messages": []} if path.startswith("/v1/inbox") else {}

    monkeypatch.setattr(served[1].client, "request", unreachable)
    served[1].published_at = float("inf")
    served[1].step(wait=0)
    assert [r["id"] for r in history.pending()] == ["a" * 32]


@pytest.mark.parametrize("answer", [
    {"results": None}, {"results": [{"id": [], "status": "recorded"}]},
    {"results": [{"id": "a" * 32, "status": []}]},
])
def test_a_malformed_answer_leaves_the_row_pending(home, served, row, monkeypatch, answer):
    _log(home, row)
    monkeypatch.setattr(served[1].client, "request", lambda method, path, body=None: answer)
    served[1].report_history()
    assert [r["id"] for r in history.pending()] == ["a" * 32]


def test_a_hub_without_the_history_route_does_not_stop_the_bridge(home, served, row, monkeypatch):
    _log(home, row)

    def refuse(*args, **kwargs):
        raise hubclient.HubRefused(404, "Not Found")

    monkeypatch.setattr(served[1].client, "request", refuse)
    assert served[1].report_history() is None


# --- the workspace view ------------------------------------------------------------------------------


@pytest.fixture
def tagged(home, served, row):
    """A local DM tagged with #release, and a later hub post on the same channel."""
    store, reporter = served
    store.hub.publish("macbook", [{"address": "codex:c1"}, {"address": "claude:c2"}])
    store.hub.join("macbook", "release", "claude:c2")
    store.send({"id": "f" * 32, "target": "#release", "text": "Kickoff from the phone."})
    _log(home, {**row, "topic": "release"})
    reporter.report_history()
    return store


def test_a_tagged_dm_appears_in_its_channel_at_its_original_time(tagged):
    assert [m["text"] for m in tagged.topic("release")["messages"]] == ["Please rerun the suite.",
                                                                          "Kickoff from the phone."]


def test_a_tagged_dm_is_marked_directed_in_the_channel(tagged):
    assert tagged.topic("release")["messages"][0]["directed"] is True


def test_a_channel_post_is_not_marked_directed(tagged):
    assert tagged.topic("release")["messages"][-1]["directed"] is False


def test_a_tagged_dm_keeps_its_recipient(tagged):
    assert tagged.topic("release")["messages"][0]["to"] == "macbook/claude:c2"


def test_a_channel_post_stays_out_of_direct_threads(tagged):
    texts = [m["text"] for thread in tagged.direct()["threads"] for m in thread["messages"]]
    assert "Kickoff from the phone." not in texts


def test_a_reported_row_says_where_it_comes_from(tagged):
    assert "From the sending machine's local message log" in tagged.topic("release")["messages"][0]["delivery"]["detail"]


def test_a_reported_row_is_flagged_for_the_view(tagged):
    assert tagged.topic("release")["messages"][0]["reported"] is True


def test_a_hub_post_has_no_reported_provenance(tagged):
    assert "local message log" not in tagged.topic("release")["messages"][-1]["delivery"]["detail"]


# --- the local log and catch-up --------------------------------------------------------------------------


def test_the_local_log_keeps_hub_sized_text(home):
    router.log_message({"id": "a" * 32, "from": "codex:c1", "to": "claude:c2", "text": "x" * 50_000})
    assert len(json.loads((home / "messages.jsonl").read_text())["text"]) == 50_000


def test_the_local_log_marks_explicit_truncation(home):
    router.log_message({"id": "a" * 32, "from": "codex:c1", "to": "claude:c2",
                        "text": "x" * (router.MESSAGE_LOG_TEXT + 1)})
    assert json.loads((home / "messages.jsonl").read_text())["truncated"] is True


def test_catch_up_keeps_a_tagged_dms_recipient(home, hub, item, monkeypatch):
    hub.report("mac", [{**item, "topic": "release"}])
    monkeypatch.setattr(team, "_hub_call", lambda work: hub.history("release"))
    assert team._hub_catch_up(object(), "release", "omp:o1", everything=True, peek=True)["messages"][0]["to"] == \
        "mac/claude:c2"


def test_catch_up_keeps_a_broadcast_without_a_recipient(home, hub, item, monkeypatch):
    hub.publish("mac", [{"address": "codex:c1"}])
    hub.send("mac", {"id": "b" * 32, "from": "codex:c1", "to": "#release", "text": "To everyone."}, join_sender=True)
    monkeypatch.setattr(team, "_hub_call", lambda work: hub.history("release"))
    assert "to" not in team._hub_catch_up(object(), "release", "omp:o1", everything=True, peek=True)["messages"][0]
