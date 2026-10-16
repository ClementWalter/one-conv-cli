"""Real CLI collectors submit only metadata over authenticated loopback HubClient requests."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from one_conv.accounting import Ledger


@pytest.fixture
def mapping(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    rows = [{"type": "session_meta", "payload": {"id": "fixture-session", "base_instructions": "private fixture marker"}},
            {"type": "turn_context", "payload": {"model": "fixture-model"}},
            {"type": "token_usage_record", "ordinal": 1, "timestamp": "2026-01-01T00:10:00Z",
             "payload": {"response_id": "resp-fixture", "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}}]
    (history / "session.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    path = tmp_path / "workers.json"
    path.write_text(json.dumps({"schema_version": 1,
                               "histories": [{"source": "codex", "path": "history", "billing_scope": "native-account"}],
                               "owners": {"codex:fixture-session": {"cost_center_id": "jack", "worker_id": "node/codex:fixture-session"}}}))
    return path


@pytest.fixture
def public_cli(tmp_path):
    def run(*arguments, environment=None):
        env = {**os.environ, "ONE_CONV_ACCOUNTING_DIR": str(tmp_path / "local-ledger")}
        env.update(environment or {})
        return subprocess.run([sys.executable, "-O", str(Path(__file__).parents[1] / "bin/one-conv"), "hr", *arguments],
                              env=env, capture_output=True, text=True, check=True, timeout=15)
    return run


def test_public_sync_imports_owned_worker(public_cli, mapping):
    result = public_cli("sync", "--mapping", str(mapping), "--local", "--json")
    assert json.loads(result.stdout)["recorded"] == 1


def test_public_watch_has_bounded_iteration(public_cli, mapping):
    result = public_cli("watch", "--mapping", str(mapping), "--local", "--iterations", "1", "--json")
    assert json.loads(result.stdout)["status"] == "synced"


def test_public_watch_repeated_pass_is_idempotent(public_cli, mapping):
    result = public_cli("watch", "--mapping", str(mapping), "--local", "--interval", "0.1", "--iterations", "2", "--json")
    assert json.loads(result.stdout.splitlines()[-1])["duplicates"] == 1


@pytest.fixture
def authority(tmp_path):
    ledger, received = Ledger(tmp_path / "authority-ledger"), []
    class AuthorityStub(BaseHTTPRequestHandler):
        """An authenticated endpoint stub persists real ledger events for transport assertions."""

        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer fixture-token":
                self.send_error(401)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            receipts = ledger.record_many({**event, "node_id": "node"} for event in body["events"])
            encoded = json.dumps({"receipts": receipts}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), AuthorityStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"ledger": ledger, "received": received, "environment": {
        "ONE_CONV_HUB_URL": f"http://127.0.0.1:{server.server_port}", "ONE_CONV_HUB_TOKEN": "fixture-token", "ONE_CONV_HUB_NODE": "node"}}
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_public_sync_posts_to_authenticated_authority(public_cli, mapping, authority):
    public_cli("sync", "--mapping", str(mapping), "--hub", "--json", environment=authority["environment"])
    assert authority["ledger"].summary("jack")["event_count"] == 1


def test_public_sync_never_posts_transcript_content(public_cli, mapping, authority):
    public_cli("sync", "--mapping", str(mapping), "--hub", "--json", environment=authority["environment"])
    assert "private fixture marker" not in json.dumps(authority["received"])


def test_public_hub_retries_do_not_double_charge(public_cli, mapping, authority):
    public_cli("sync", "--mapping", str(mapping), "--hub", "--json", environment=authority["environment"])
    result = public_cli("sync", "--mapping", str(mapping), "--hub", "--json", environment=authority["environment"])
    assert json.loads(result.stdout)["duplicates"] == 1
