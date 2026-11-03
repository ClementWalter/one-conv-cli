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
"""A staffing node pulls the controller's state and an incremental archive mirror, and a fresh
controller directory is rebuilt from them."""

import io
import json
import sqlite3
import tarfile

import pytest
from click.testing import CliRunner
from starlette.requests import Request
from starlette.testclient import TestClient

from archive.store import SQLiteStore
from one_conv.control import backup
from one_conv.control.cli import build_control_group
from one_conv.control.runtime import application
from one_conv.control.store import ControlStore
from one_conv.live import bridge, controller_backup, hubclient

ORIGIN = "https://workspace.example.test"


def bearer(token):
    return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})


class StubHub:
    """HubClient's request/download over the in-process app instead of urllib."""

    def __init__(self, http, token):
        self.http, self.headers = http, {"Authorization": f"Bearer {token}"}

    def _get(self, path):
        response = self.http.get(path, headers=self.headers)
        if response.status_code >= 400:
            raise hubclient.HubRefused(response.status_code, response.json().get("error", ""))
        return response

    def request(self, method, path, body=None):
        return self._get(path).json()

    def download(self, path):
        response = self._get(path)
        return response.headers["content-type"], response.content


@pytest.fixture
def controller(tmp_path):
    return tmp_path / "control"


@pytest.fixture
def app(controller):
    return application(controller, ORIGIN, worker_config={})


@pytest.fixture
def http(app):
    return TestClient(app, base_url=ORIGIN)


@pytest.fixture
def archive_store(app, controller):
    store = SQLiteStore(f"sqlite:///{controller / backup.ARCHIVE_DB}")
    store.put_chunks({"a" * 64: b"first chunk", "b" * 64: b"second chunk"})
    store.put("macbook/claude:one", "r1", {"mtime": 1}, "hello", {"chunks": ["a" * 64]}, None)
    yield store
    store.engine.dispose()


@pytest.fixture
def staffing_token(app):
    token = app.state.hub.add_token("macbook")
    app.state.store.grant_staffing("macbook")
    return token


@pytest.fixture
def hub(http, staffing_token, archive_store):
    return StubHub(http, staffing_token)


@pytest.fixture
def pulled(hub, tmp_path):
    target = tmp_path / "pulled"
    controller_backup.run(hub, target)
    return target


def mirror_rows(target, sql):
    with sqlite3.connect(target / "archive.db") as db:
        return db.execute(sql).fetchall()


def snapshot_names(target):
    [snapshot] = (target / "state").glob("state-*.tar.gz")
    with tarfile.open(snapshot) as tar:
        return set(tar.getnames())


def test_an_ordinary_node_cannot_pull_the_state(app, http):
    token = app.state.hub.add_token("laptop")
    assert http.get("/v1/backup/state", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_an_ordinary_node_cannot_pull_the_archive(app, http):
    token = app.state.hub.add_token("laptop")
    assert http.get("/v1/backup/archive?table=chunks_v1", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_an_unknown_archive_table_is_refused(http, staffing_token, archive_store):
    assert http.get("/v1/backup/archive?table=sqlite_master",
                    headers={"Authorization": f"Bearer {staffing_token}"}).status_code == 400


def test_the_state_snapshot_holds_the_hub(pulled):
    assert "hub.sqlite3" in snapshot_names(pulled)


def test_the_state_snapshot_leaves_the_archive_out(pulled):
    assert not any(name.startswith("archive") for name in snapshot_names(pulled))


def test_the_mirror_holds_every_chunk(pulled):
    assert mirror_rows(pulled, "SELECT digest FROM chunks_v1 ORDER BY digest") == [("a" * 64,), ("b" * 64,)]


def test_the_mirror_holds_the_head(pulled):
    assert mirror_rows(pulled, "SELECT key, revision FROM conversations_v1") == [("macbook/claude:one", "r1")]


def test_a_second_pull_fetches_only_new_chunks(hub, pulled, archive_store):
    archive_store.put_chunks({"c" * 64: b"third chunk"})
    assert controller_backup.run(hub, pulled)["archive"]["chunks_v1"] == 1


def test_a_moved_head_moves_in_the_mirror(hub, pulled, archive_store):
    archive_store.put("macbook/claude:one", "r2", {"mtime": 2}, "hello again", {"chunks": []}, "r1")
    controller_backup.run(hub, pulled)
    assert mirror_rows(pulled, "SELECT revision FROM conversations_v1") == [("r2",)]


def test_a_page_stops_at_its_byte_budget(controller, archive_store):
    assert backup.archive_page(controller / backup.ARCHIVE_DB, "chunks_v1", 0, max_bytes=1)["more"] is True


def test_an_oversized_row_still_moves_the_cursor(controller, archive_store):
    assert len(backup.archive_page(controller / backup.ARCHIVE_DB, "chunks_v1", 0, max_bytes=1)["rows"]) == 1


def test_small_pages_still_mirror_every_chunk(hub, tmp_path, archive_store, monkeypatch):
    real = backup.archive_page
    monkeypatch.setattr(backup, "archive_page", lambda database, table, after: real(database, table, after, max_bytes=1))
    controller_backup.run(hub, tmp_path / "pulled")
    assert mirror_rows(tmp_path / "pulled", "SELECT count(*) FROM chunks_v1") == [(2,)]


def test_a_replaced_archive_starts_a_fresh_mirror(hub, pulled, controller):
    with sqlite3.connect(controller / backup.ARCHIVE_DB) as db:
        db.execute("UPDATE archive_state_v1 SET generation = 'replaced'")
    controller_backup.run(hub, pulled)
    assert (pulled / "archive.previous.db").exists()


def test_old_state_snapshots_are_dropped(hub, tmp_path, archive_store):
    for hour in range(4):
        controller_backup.save_state(hub, tmp_path, keep=2, now=1_800_000_000 + hour * 3600)
    assert len(list((tmp_path / "state").glob("state-*.tar.gz"))) == 2


def test_a_restored_controller_accepts_the_old_node_token(pulled, staffing_token, tmp_path):
    backup.restore(pulled, tmp_path / "restored")
    assert ControlStore(tmp_path / "restored").hub.authenticated_node(bearer(staffing_token)) == "macbook"


def test_a_restored_archive_serves_the_conversation(pulled, tmp_path):
    backup.restore(pulled, tmp_path / "restored")
    store = SQLiteStore(f"sqlite:///{tmp_path / 'restored' / backup.ARCHIVE_DB}")
    assert store.read_conversation("macbook/claude:one") == {"chunks": ["a" * 64]}


def test_a_restored_archive_has_a_new_generation(pulled, tmp_path, controller):
    backup.restore(pulled, tmp_path / "restored")
    with sqlite3.connect(tmp_path / "restored" / backup.ARCHIVE_DB) as db:
        restored = db.execute("SELECT generation FROM archive_state_v1").fetchone()
    with sqlite3.connect(controller / backup.ARCHIVE_DB) as db:
        original = db.execute("SELECT generation FROM archive_state_v1").fetchone()
    assert restored != original


def test_restore_refuses_a_directory_with_a_hub(pulled, controller):
    with pytest.raises(FileExistsError):
        backup.restore(pulled, controller)


def test_the_restore_command_reports_what_it_restored(pulled, tmp_path):
    result = CliRunner().invoke(build_control_group(), ["--directory", str(tmp_path / "restored"), "restore",
                                                        "--from", str(pulled), "--json"])
    assert json.loads(result.output)["archive"] is True


def test_a_restored_state_is_private(pulled, tmp_path):
    backup.restore(pulled, tmp_path / "restored")
    assert (tmp_path / "restored" / "hub.sqlite3").stat().st_mode & 0o077 == 0


def test_the_bridge_backup_is_off_at_zero_minutes(tmp_path):
    assert bridge.ControllerBackup(minutes=0, launcher=tmp_path / "launcher.py").poll("https://h", "t", "macbook") is False


def test_the_bridge_backs_up_hourly_by_default(monkeypatch):
    monkeypatch.delenv("ONE_CONV_CONTROLLER_BACKUP_MINUTES", raising=False)
    assert bridge.ControllerBackup().seconds == 3600


def test_a_tar_cannot_write_outside_the_controller(tmp_path):
    source = tmp_path / "evil"
    (source / "state").mkdir(parents=True)
    with tarfile.open(source / "state" / "state-20260101T000000Z.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo("../escaped")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(tarfile.FilterError):
        backup.restore(source, tmp_path / "restored")
