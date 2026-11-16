"""The Cloudflare Worker relay (relay-worker/) as a second backend for the relay and archive tests.

With ONE_CONV_TEST_RELAY_WORKER=1, fixtures parametrized over BACKENDS also run against the
Worker: `wrangler dev --local` starts once per test process (no Cloudflare account involved)
and is emptied before each test, so the same assertions hold for the Python relay and the Worker.
RemoteHub answers the Hub methods tests call directly, through the Worker's HTTP API.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from one_conv.live.hub import HubError

WORKER_DIR = Path(__file__).resolve().parents[1] / "relay-worker"
ENABLED = os.environ.get("ONE_CONV_TEST_RELAY_WORKER") == "1"
BACKENDS = ["python", *(["worker"] if ENABLED else [])]
ARCHIVE_SECRET = "test-secret"
# The first start compiles the Worker and boots workerd.
START_SECONDS = 90


def _request(url: str, method: str = "GET", token: str | None = None, body: bytes | None = None,
             content_type: str = "application/json", timeout: float = 60) -> tuple[int, str, bytes]:
    """(status, content type, body) of one request; HTTP errors are returned, not raised."""
    request = Request(url, data=body, method=method, headers={"Authorization": f"Bearer {token}"} if token else {})
    if body is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class WorkerProcess:
    """`wrangler dev --local` on a free loopback port, with its own state directory and admin token."""

    def __init__(self):
        wrangler = WORKER_DIR / "node_modules" / ".bin" / "wrangler"
        if not wrangler.exists():
            pytest.fail(f"run `npm ci` in {WORKER_DIR} before ONE_CONV_TEST_RELAY_WORKER=1")
        self.state = Path(tempfile.mkdtemp(prefix="relay-worker-"))
        # The admin token is the Worker's counterpart of the standalone archive's shared token,
        # which test_archive.py names "test-secret"; the server only ever listens on loopback.
        self.admin = ARCHIVE_SECRET
        port, inspector = _free_port(), _free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.log = (self.state / "wrangler.log").open("w")
        # Its own process group: wrangler starts workerd children that must stop with it.
        self.process = subprocess.Popen(
            [str(wrangler), "dev", "--local", "--ip", "127.0.0.1", "--port", str(port), "--inspector-port", str(inspector), "--persist-to",
             str(self.state / "data"), "--var", f"RELAY_ADMIN_TOKEN:{self.admin}", "--var", "RELAY_ALLOW_RESET:1",
             "--show-interactive-dev-session=false"],
            cwd=WORKER_DIR, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "WRANGLER_SEND_METRICS": "false"})
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            try:
                if _request(self.url + "/v1/whoami", timeout=2)[0] == 401:
                    return
            except (URLError, OSError):
                pass
            if self.process.poll() is not None:
                break
            time.sleep(0.2)
        log = (self.state / "wrangler.log").read_text()[-2000:]
        self.stop()
        pytest.fail(f"wrangler dev did not start:\n{log}")

    def admin_call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        status, _, data = _request(self.url + path, method, self.admin, json.dumps(body).encode() if body is not None else None)
        return status, json.loads(data)

    def reset(self) -> None:
        status, body = self.admin_call("POST", "/admin/reset")
        assert status == 200, body

    def hub(self) -> "RemoteHub":
        self.reset()
        return RemoteHub(self)

    def stop(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.log.close()
        shutil.rmtree(self.state, ignore_errors=True)


class RemoteHub:
    """The Hub methods tests call, answered by the Worker; errors raise HubError as Hub does."""

    def __init__(self, worker: WorkerProcess):
        self.worker, self.url, self.tokens = worker, worker.url, {}

    def add_token(self, node: str) -> str:
        status, body = self.worker.admin_call("POST", "/admin/tokens", {"node": node})
        if status != 200:
            raise HubError(status, body["error"])
        self.tokens[node] = body["token"]
        return body["token"]

    def revoke(self, node: str) -> bool:
        self.tokens.pop(node, None)
        return self.worker.admin_call("DELETE", f"/admin/tokens/{quote(node, safe='')}")[0] == 200

    def _token(self, node: str) -> str:
        return self.tokens.get(node) or self.add_token(node)

    def _call(self, node: str, method: str, path: str, body: dict | None = None):
        status, _, data = _request(self.url + path, method, self._token(node),
                                   json.dumps(body).encode() if body is not None else None)
        parsed = json.loads(data)
        if status != 200:
            raise HubError(status, parsed["error"])
        return parsed

    def publish(self, node: str, sessions) -> int:
        return self._call(node, "PUT", "/v1/sessions", {"sessions": sessions})["published"]

    def directory(self) -> list[dict]:
        return self._call(next(iter(self.tokens), "observer"), "GET", "/v1/sessions")["sessions"]

    def send(self, node: str, message: dict) -> dict:
        return self._call(node, "POST", "/v1/messages", message)

    def inbox(self, node: str, wait: float) -> list[dict]:
        return self._call(node, "GET", f"/v1/inbox?wait={wait}")["messages"]

    def ack(self, node: str, delivery: str, outcome: str) -> dict:
        return self._call(node, "POST", "/v1/acks", {"delivery": delivery, "outcome": outcome})

    def status(self, node: str, msg_id: str) -> dict:
        return self._call(node, "GET", f"/v1/messages/{msg_id}")

    def store_attachment(self, node: str, media_type: str, body: bytes) -> dict:
        status, _, data = _request(self.url + "/v1/attachments", "POST", self._token(node), body, media_type)
        if status != 200:
            raise HubError(status, json.loads(data)["error"])
        return json.loads(data)

    def attachment(self, ident: str) -> tuple[str, bytes]:
        status, content_type, data = _request(self.url + f"/v1/attachments/{ident}", token=self._token(next(iter(self.tokens), "observer")))
        if status != 200:
            raise HubError(status, json.loads(data)["error"])
        return content_type.partition(";")[0], data


def python_only(state, reason: str) -> None:
    """Skip a test that reaches into the Python relay itself; relay-worker/test covers the Worker side."""
    if isinstance(state, RemoteHub):
        pytest.skip(reason)


def local_database(state) -> str:
    """The Python relay's SQLite path; a test that edits it directly does not apply to the Worker."""
    python_only(state, "edits the Python relay's SQLite file")
    return state.path
