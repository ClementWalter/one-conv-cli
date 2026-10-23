"""Routines: cron matching, the workspace posting a routine's prompt in its channel, and its controls."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from one_conv.control.routines import Cron, describe, fire_id, validate
from one_conv.control.store import ControlStore
from one_conv.live.hub import HubError

PARIS = ZoneInfo("Europe/Paris")


def at(text, tz=PARIS):
    return datetime.fromisoformat(text).replace(tzinfo=tz).timestamp()


def local(seconds, tz=PARIS):
    return datetime.fromtimestamp(seconds, tz).strftime("%Y-%m-%d %H:%M %a")


@pytest.mark.parametrize("line, after, expected", [
    ("0 21 * * *", "2026-10-23 10:00", "2026-10-23 21:00 Fri"),
    ("0 21 * * *", "2026-10-23 21:00", "2026-10-24 21:00 Sat"),
    ("30 9 * * 1-5", "2026-10-23 10:00", "2026-10-26 09:30 Mon"),
    ("0 8 * * 1", "2026-10-23 10:00", "2026-10-26 08:00 Mon"),
    ("0 8 * * 7", "2026-10-23 10:00", "2026-10-25 08:00 Sun"),
    ("*/15 * * * *", "2026-10-23 10:07", "2026-10-23 10:15 Fri"),
    ("15 9 2 * *", "2026-10-23 10:00", "2026-11-02 09:15 Mon"),
    ("0 0 1 * 5", "2026-10-23 10:00", "2026-10-30 00:00 Fri"),
    ("0 9 29 2 *", "2026-10-23 10:00", "2028-02-29 09:00 Tue"),
])
def test_next_fire_time(line, after, expected):
    assert local(Cron(line).next_after(at(after), PARIS)) == expected


def test_a_daily_time_stays_local_across_the_clock_change():
    assert local(Cron("0 21 * * *").next_after(at("2026-10-25 01:00"), PARIS)) == "2026-10-25 21:00 Sun"


def test_a_schedule_that_never_comes_round_has_no_next_time():
    assert Cron("0 9 30 2 *").next_after(at("2026-10-23 10:00"), PARIS) is None


@pytest.mark.parametrize("line", ["0 21 * *", "61 * * * *", "* 24 * * *", "0 9 * * mon", "*/0 * * * *"])
def test_a_malformed_cron_line_is_refused(line):
    with pytest.raises(ValueError):
        Cron(line)


@pytest.mark.parametrize("line, words", [
    ("0 21 * * *", "Every day at 21:00"),
    ("30 9 * * 1-5", "Weekdays at 09:30"),
    ("0 8 * * 1", "Mondays at 08:00"),
    ("5 * * * *", "Every hour at :05"),
    ("15 9 2 * *", "cron 15 9 2 * *"),
])
def test_schedule_in_words(line, words):
    assert describe(line) == words


@pytest.fixture
def routine():
    return {"id": "b" * 32, "name": "Daily review", "topic": "reviews", "prompt": "Review the day.",
            "cron": "0 21 * * *", "tz": "Europe/Paris", "enabled": True}


@pytest.mark.parametrize("change, status", [
    ({"tz": "Mars/Olympus"}, 400),
    ({"topic": "Not A Channel"}, 400),
    ({"cron": "0 9 30 2 *"}, 400),
    ({"prompt": "  "}, 400),
    ({"name": ""}, 400),
    ({"id": "short"}, 400),
    ({"enabled": "yes"}, 400),
])
def test_an_invalid_routine_is_refused(routine, change, status):
    with pytest.raises(HubError) as error:
        validate({**routine, **change})
    assert error.value.status == status


@pytest.fixture
def store(tmp_path):
    return ControlStore(tmp_path)


def channel_texts(store, topic="reviews"):
    return [row["text"] for row in store.topic(topic)["messages"]]


def test_a_saved_routine_is_listed_with_its_next_run(store, routine):
    store.routines.save(routine)
    assert store.state()["routines"][0]["next_at"] > 0


def test_a_saved_routine_is_listed_in_words(store, routine):
    store.routines.save(routine)
    assert store.state()["routines"][0]["schedule"] == "Every day at 21:00"


@pytest.fixture
def fired(store, routine):
    saved = store.routines.save(routine)
    return store, store.routines.fire_due(saved["next_at"] + 1)


def test_a_due_routine_posts_its_prompt_in_its_channel(fired):
    store, _ = fired
    assert channel_texts(store)[-1].endswith("Review the day.")


def test_a_routine_post_names_the_routine(fired):
    store, _ = fired
    assert channel_texts(store)[-1].startswith("⏰ Routine “Daily review” (Every day at 21:00, Europe/Paris)")


def test_a_routine_post_is_the_human_s(fired):
    store, _ = fired
    assert store.topic("reviews")["messages"][-1]["from"] == "user"


def test_a_fired_routine_moves_to_its_next_day(fired, routine):
    store, _ = fired
    row = store.routines.get(routine["id"])
    assert row["next_at"] - row["last_at"] > 3600


def test_a_retried_fire_posts_once(store, routine):
    saved = store.routines.save(routine)
    store.send({"id": fire_id(routine["id"], saved["next_at"]), "target": "#reviews", "text": "earlier attempt"})
    store.routines.fire_due(saved["next_at"] + 1)
    assert len(channel_texts(store)) == 1


def test_fires_missed_while_down_run_once(store, routine):
    saved = store.routines.save(routine)
    store.routines.fire_due(saved["next_at"] + 5 * 86400)
    store.routines.fire_due(saved["next_at"] + 5 * 86400 + 30)
    assert len(channel_texts(store)) == 1


def test_a_routine_that_is_not_due_does_not_post(store, routine):
    saved = store.routines.save(routine)
    assert store.routines.fire_due(saved["next_at"] - 60) == []


def test_a_paused_routine_does_not_post(store, routine):
    saved = store.routines.save(routine)
    store.routines.act({"id": routine["id"], "action": "pause"})
    assert store.routines.fire_due(saved["next_at"] + 1) == []


def test_resuming_schedules_the_routine_again(store, routine):
    store.routines.save({**routine, "enabled": False})
    assert store.routines.act({"id": routine["id"], "action": "resume"})["next_at"] > 0


def test_run_now_posts_at_once(store, routine):
    store.routines.save(routine)
    store.routines.act({"id": routine["id"], "action": "run"})
    assert channel_texts(store)[-1].endswith("Review the day.")


def test_run_now_keeps_the_schedule(store, routine):
    saved = store.routines.save(routine)
    assert store.routines.act({"id": routine["id"], "action": "run"})["next_at"] == saved["next_at"]


def test_saving_again_under_the_same_id_replaces_the_routine(store, routine):
    store.routines.save(routine)
    store.routines.save({**routine, "name": "Evening review"})
    assert [row["name"] for row in store.routines.listing()] == ["Evening review"]


def test_a_deleted_routine_is_gone(store, routine):
    store.routines.save(routine)
    store.routines.act({"id": routine["id"], "action": "delete"})
    assert store.routines.listing() == []


def test_an_unknown_action_is_refused(store, routine):
    store.routines.save(routine)
    with pytest.raises(HubError):
        store.routines.act({"id": routine["id"], "action": "explode"})


def test_saving_a_routine_changes_the_workspace_fingerprint(store, routine):
    before = store.fingerprint()
    store.routines.save(routine)
    assert store.fingerprint() != before
