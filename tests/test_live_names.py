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
"""Agent names: binding, collisions, sending to @name and the sender's handle in the envelope."""

import json
import os

import pytest

from one_conv.live import names, router
from one_conv.live.model import LiveSession


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("omp", "OMP_HOME"),
                      ("cursor", "ONE_CONV_CURSOR_HOME"), ("live", "ONE_CONV_LIVE_DIR")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    return tmp_path


def nobody_running(address):
    return False


def _omp(homes, session_id, pid=None):
    (homes / "live" / "omp").mkdir(exist_ok=True)
    (homes / "live" / "omp" / f"{session_id}.json").write_text(
        json.dumps({"pid": pid or os.getppid(), "sessionId": session_id, "cwd": f"/work/{session_id}"}))


def _inbox(homes, session_id):
    directory = homes / "live" / "inbox" / "omp" / session_id
    return [json.loads(p.read_text())["text"] for p in sorted(directory.glob("*.json"))] if directory.is_dir() else []


def test_name_resolves_to_its_address(homes):
    names.set_name("reviewer", "omp:o1", nobody_running)
    assert names.address_for("@reviewer") == "omp:o1"


def test_name_held_by_a_running_session_is_refused(homes):
    names.set_name("reviewer", "omp:o1", nobody_running)
    with pytest.raises(names.NameRefused, match="running"):
        names.set_name("reviewer", "omp:o2", lambda address: True)


def test_name_held_by_a_stopped_session_needs_replace(homes):
    names.set_name("reviewer", "omp:o1", nobody_running)
    with pytest.raises(names.NameRefused, match="--replace"):
        names.set_name("reviewer", "omp:o2", nobody_running)


def test_replace_takes_a_stopped_sessions_name(homes):
    names.set_name("reviewer", "omp:o1", nobody_running)
    names.set_name("reviewer", "omp:o2", nobody_running, replace=True)
    assert names.address_for("reviewer") == "omp:o2"


def test_renaming_releases_the_old_name(homes):
    names.set_name("reviewer", "omp:o1", nobody_running)
    names.set_name("builder", "omp:o1", nobody_running)
    assert names.names() == {"builder": "omp:o1"}


@pytest.mark.parametrize("bad", ["", "-lead", "a" * 33, "has space", "dot.name", "../up", "UPPER$"])
def test_invalid_name_is_refused(homes, bad):
    with pytest.raises(names.NameRefused):
        names.set_name(bad, "omp:o1", nobody_running)


def test_send_to_a_name_reaches_its_session(homes):
    _omp(homes, "o1")
    names.set_name("reviewer", "omp:o1", nobody_running)
    router.send("@reviewer", "please look", LiveSession("claude", "c1"))
    assert "please look" in _inbox(homes, "o1")[0]


def test_send_to_an_unknown_name_fails_visibly(homes):
    with pytest.raises(router.LiveError, match="No agent is called"):
        router.send("@ghost", "hi", LiveSession("claude", "c1"))


def test_envelope_carries_the_senders_handle(homes):
    _omp(homes, "o1")
    names.set_name("builder", "claude:c1", nobody_running)
    router.send("omp:o1", "hi", LiveSession("claude", "c1"))
    assert 'from-handle="@builder"' in _inbox(homes, "o1")[0]


def test_reply_still_routes_by_address_not_name(homes):
    _omp(homes, "o1")
    names.set_name("builder", "claude:c1", nobody_running)
    router.send("omp:o1", "hi", LiveSession("claude", "c1"))
    assert 'to="claude:c1"' in _inbox(homes, "o1")[0]
