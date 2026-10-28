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
"""Local workspace unread counts: the human's read marks live beside the send journal, not in the browser."""

import http.client
import json
import stat
import time

import pytest

pytest.importorskip("uvicorn")

from one_conv.live import router, team, web
from one_conv.live.model import LiveSession
from tests import test_live_web as live_tests

homes = live_tests.homes
viewer = live_tests.viewer
human_headers = live_tests.human_headers
fetch = live_tests.fetch


@pytest.fixture
def started(homes):
    """Read tracking starts before anything is posted, so every later agent message is unread."""
    web.read_marks()
    time.sleep(0.01)
    return homes


def topic_unread():
    return next(topic["unread"] for topic in web.state()["topics"] if topic["topic"] == "release")


def test_agent_post_in_a_topic_is_unread(started):
    router.send("#release", "first", LiveSession("claude", "c1"))
    assert topic_unread() == 1


def test_history_before_read_tracking_starts_read(homes):
    router.send("#release", "first", LiveSession("claude", "c1"))
    assert topic_unread() == 0


def test_human_post_in_a_topic_is_not_unread(started):
    team.join("release", "omp:o1")
    team.record("release", "a" * 32, "user", "From me.")
    assert topic_unread() == 0


def test_marking_a_topic_read_clears_it(started):
    router.send("#release", "first", LiveSession("claude", "c1"))
    web.mark_read({"conversation": "#release", "at": web.topic_messages("release")["messages"][-1]["at"]})
    assert topic_unread() == 0


def test_agent_direct_message_is_unread(started):
    router.log_message({"id": "b" * 32, "from": "omp:o1", "to": "user", "text": "Hello."})
    assert web.direct()["threads"][0]["unread"] == 1


def test_marking_a_direct_thread_read_clears_it(started):
    router.log_message({"id": "b" * 32, "from": "omp:o1", "to": "user", "text": "Hello."})
    thread = web.direct()["threads"][0]
    web.mark_read({"conversation": thread["pair"], "at": thread["last"]})
    assert web.direct()["threads"][0]["unread"] == 0


def test_read_marks_are_owner_only(started):
    assert stat.S_IMODE((started / "live" / "web-read.sqlite3").stat().st_mode) == 0o600


def post_read(port, headers, payload):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("POST", "/api/read", body=json.dumps(payload).encode(), headers=headers)
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


@pytest.fixture
def read_mark():
    return {"conversation": "#release", "at": 10.0}


def test_session_advertises_unread(viewer):
    assert json.loads(fetch(viewer, "/api/session")[2])["capabilities"] == {"unread": True}


def test_read_mark_is_saved_over_http(viewer, human_headers, read_mark):
    assert post_read(viewer, human_headers, read_mark) == (200, read_mark)


@pytest.mark.parametrize(("key", "value"), [
    ("X-One-Conv-Token", None), ("X-One-Conv-Token", "wrong"), ("Origin", None), ("Origin", "http://attacker.example"),
])
def test_read_mark_requires_own_origin_and_token(viewer, human_headers, read_mark, key, value):
    if value is None:
        human_headers.pop(key)
    else:
        human_headers[key] = value
    assert post_read(viewer, human_headers, read_mark)[0] == 403


def test_invalid_read_mark_is_a_client_error(viewer, human_headers):
    assert post_read(viewer, human_headers, {"conversation": "#Bad", "at": 1})[0] == 400


def test_read_mark_requires_json(viewer, human_headers, read_mark):
    human_headers["Content-Type"] = "text/plain"
    assert post_read(viewer, human_headers, read_mark)[0] == 415
