"""Read marks: validation, counting and the forward-only mark shared by both workspaces."""

import sqlite3
import time

import pytest

from one_conv.live import unread


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    unread.ensure(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize("conversation", ["#release", "omp:a user", "macbook/claude:x workspace/user"])
def test_valid_conversation_is_accepted(conversation):
    assert unread.validate({"conversation": conversation, "at": 1.0})["conversation"] == conversation


@pytest.mark.parametrize("payload", [
    None, [], {}, {"conversation": "#release"}, {"at": 1.0},
    {"conversation": "#release", "at": 1.0, "extra": 1},
    {"conversation": "", "at": 1.0}, {"conversation": 7, "at": 1.0},
    {"conversation": "#Release", "at": 1.0}, {"conversation": "#", "at": 1.0},
    {"conversation": "user omp:a", "at": 1.0}, {"conversation": "omp:a", "at": 1.0},
    {"conversation": "a b c", "at": 1.0}, {"conversation": "a  b", "at": 1.0},
    {"conversation": "a\tb user", "at": 1.0}, {"conversation": "#x user", "at": 1.0},
    {"conversation": "a " + "b" * 1100, "at": 1.0},
    {"conversation": "#release", "at": "1"}, {"conversation": "#release", "at": True},
    {"conversation": "#release", "at": -1}, {"conversation": "#release", "at": float("nan")},
    {"conversation": "#release", "at": float("inf")},
])
def test_invalid_read_mark_is_refused(payload):
    with pytest.raises(unread.ReadError):
        unread.validate(payload)


def test_future_mark_is_clamped_to_now():
    assert unread.validate({"conversation": "#release", "at": time.time() + 3600})["at"] <= time.time()


@pytest.mark.parametrize("address", ["user", "workspace/user", "macbook/user"])
def test_human_addresses_are_recognised(address):
    assert unread.is_human(address)


@pytest.mark.parametrize("address", ["omp:user", "users", "a/b/user", None])
def test_agent_addresses_are_not_human(address):
    assert not unread.is_human(address)


@pytest.fixture
def messages():
    return [{"from": "omp:a", "at": 10.0}, {"from": "user", "at": 20.0}, {"from": "omp:a", "at": 30.0},
            {"from": "workspace/user", "at": 40.0}, {"from": "omp:b", "at": "bad"}]


def test_count_skips_the_humans_own_messages(messages):
    assert unread.count(messages, 0.0) == 2


def test_count_skips_messages_up_to_the_mark(messages):
    assert unread.count(messages, 10.0) == 1


def test_ensure_records_a_baseline(db):
    assert unread.marks(db)[0] > 0


def test_ensure_keeps_the_first_baseline(db):
    before = unread.marks(db)[0]
    unread.ensure(db)
    assert unread.marks(db)[0] == before


def test_baseline_is_not_a_conversation(db):
    assert unread.marks(db)[1] == {}


def test_mark_is_stored(db):
    unread.mark(db, {"conversation": "#release", "at": 5.0})
    assert unread.marks(db)[1] == {"#release": 5.0}


def test_mark_never_moves_back(db):
    unread.mark(db, {"conversation": "#release", "at": 5.0})
    assert unread.mark(db, {"conversation": "#release", "at": 1.0}) == 5.0
