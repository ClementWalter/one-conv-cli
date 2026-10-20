"""The local live workspace over real HTTP, including peeking and human delivery."""

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from one_conv.live import router, team, web, web_send
from one_conv.live.model import LiveSession, envelope


@pytest.fixture
def homes(tmp_path, monkeypatch):
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("omp", "OMP_HOME"),
                      ("cursor", "ONE_CONV_CURSOR_HOME"), ("live", "ONE_CONV_LIVE_DIR")):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(var, str(tmp_path / name))
    for name in ("ONE_CONV_HUB_URL", "ONE_CONV_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _omp(homes, session_id):
    (homes / "live" / "omp").mkdir(exist_ok=True)
    (homes / "live" / "omp" / f"{session_id}.json").write_text(
        json.dumps({"pid": os.getppid(), "sessionId": session_id, "cwd": f"/work/{session_id}"}))


@pytest.fixture
def viewer(homes):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(web.application(port), log_level="error", access_log=False, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.02)
    yield port
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


def fetch(port, path, method="GET", host=None):
    """(status, headers, body) of one request, with an explicit Host header."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    response = connection.getresponse()
    return response.status, dict(response.getheaders()), response.read()


# --- message log ---------------------------------------------------------------------------


def test_send_logs_the_message_text(homes):
    _omp(homes, "o1")
    router.send("omp:o1", "please review", LiveSession("claude", "c1"))
    assert web.log()[-1]["text"] == "please review"


def test_topic_send_is_logged_once_with_per_member_status(homes):
    for session in ("o1", "o2"):
        _omp(homes, session)
        team.join("release", f"omp:{session}")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert sorted(web.log()[-1]["status"]) == ["omp:o1", "omp:o2"]


def test_log_keeps_only_the_latest_messages(homes, monkeypatch):
    monkeypatch.setattr(router, "MESSAGE_LOG", 3)
    for index in range(5):
        router.log_message({"id": str(index), "text": f"m{index}"})
    assert [row["id"] for row in web.log()] == ["2", "3", "4"]


def test_log_failure_does_not_fail_the_send(homes, monkeypatch):
    _omp(homes, "o1")

    def broken(row):
        raise OSError("disk full")

    monkeypatch.setattr(router, "log_message", broken)
    assert router.send("omp:o1", "hi", LiveSession("claude", "c1"))["status"] == "delivered"


def test_direct_view_groups_messages_by_pair(homes):
    _omp(homes, "o1")
    router.send("omp:o1", "one", LiveSession("claude", "c1"))
    router.send("omp:o1", "two", LiveSession("claude", "c1"))
    assert [len(t["messages"]) for t in web.direct()["threads"]] == [2]


def test_direct_view_leaves_out_topic_messages(homes):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    router.send("#release", "tests pass", LiveSession("claude", "c1"))
    assert web.direct()["threads"] == []


def test_corrupt_log_line_does_not_hide_the_rest(homes):
    router.log_message({"id": "a", "text": "kept"})
    with (homes / "live" / "messages.jsonl").open("a") as stream:
        stream.write("{not json\n")
    assert [row["text"] for row in web.log()] == ["kept"]


# --- HTTP ------------------------------------------------------------------------------------


def test_state_lists_topics(viewer, homes):
    team.join("release", "omp:o1")
    _, _, body = fetch(viewer, "/api/state")
    assert [t["topic"] for t in json.loads(body)["topics"]] == ["release"]


def test_topic_endpoint_returns_its_history(viewer, homes):
    router.send("#release", "first", LiveSession("claude", "c1"))
    _, _, body = fetch(viewer, "/api/topics/release")
    assert [m["text"] for m in json.loads(body)["messages"]] == ["first"]


def test_viewing_a_topic_does_not_move_any_readers_position(viewer, homes):
    team.join("release", "omp:o1")
    router.send("#release", "first", LiveSession("claude", "c1"))
    fetch(viewer, "/api/topics/release")
    assert [m["text"] for m in team.catch_up("release", "omp:o1")["messages"]] == ["first"]


def test_foreign_host_header_is_refused(viewer):
    status, _, _ = fetch(viewer, "/api/state", host="attacker.example")
    assert status == 421


def test_writes_are_not_accepted(viewer):
    status, _, _ = fetch(viewer, "/api/state", method="POST")
    assert status == 405


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/state", "/api/session"])
def test_responses_carry_a_strict_content_policy(viewer, path):
    _, headers, _ = fetch(viewer, path)
    assert headers["content-security-policy"].startswith("default-src 'none'; script-src 'self'")


def test_invalid_topic_is_a_client_error(viewer):
    status, _, _ = fetch(viewer, "/api/topics/bad%20topic")
    assert status == 400


def test_topic_with_a_slash_matches_no_route(viewer):
    status, _, _ = fetch(viewer, "/api/topics/..%2Fetc")
    assert status == 404


def test_page_script_never_writes_html():
    assert "innerHTML" not in web.SCRIPT and "outerHTML" not in web.SCRIPT and "insertAdjacentHTML" not in web.SCRIPT


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_script_parses(tmp_path):
    script = tmp_path / "app.js"
    script.write_text(web.SCRIPT)
    assert subprocess.run(["node", "--check", str(script)], capture_output=True, check=False).returncode == 0


def test_fyi_is_logged_as_expecting_no_reply(homes):
    _omp(homes, "o1")
    router.send("omp:o1", "merged", LiveSession("claude", "c1"), fyi=True)
    assert web.log()[-1]["reply"] == "none"


def test_page_marks_fyi_rows():
    assert 'row.mode === "fyi"' in web.SCRIPT


# --- human messages ------------------------------------------------------------------------


@pytest.fixture
def human_payload():
    return {"id": "1" * 32, "target": "omp:o1", "text": "Please review the release."}


@pytest.fixture
def permissive_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_journal_is_owner_only_with_a_permissive_umask(homes, human_payload, permissive_umask):
    web_send._claim(human_payload)
    assert (homes / "live" / "web-messages.sqlite3").stat().st_mode & 0o777 == 0o600


def test_existing_journal_permissions_are_tightened(homes, human_payload):
    web_send._claim(human_payload)
    path = homes / "live" / "web-messages.sqlite3"
    path.chmod(0o644)
    web_send._claim(human_payload)
    assert path.stat().st_mode & 0o777 == 0o600


def test_existing_journal_claims_survive_permission_changes(homes, human_payload):
    web_send._claim(human_payload)
    (homes / "live" / "web-messages.sqlite3").chmod(0o644)
    assert web_send._claim(human_payload)[0] is False


@pytest.fixture
def human_headers(viewer):
    _, _, body = fetch(viewer, "/api/session")
    return {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{viewer}",
            "X-One-Conv-Token": json.loads(body)["csrf_token"]}


def post_message(port, headers, payload=None, body=None, chunked=False):
    """Exercise the actual HTTP parser, including streamed requests without a length."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(payload).encode() if body is None else body
    connection.request("POST", "/api/messages", body=data, headers=headers, encode_chunked=chunked)
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


def test_session_identifies_the_human(viewer):
    _, _, body = fetch(viewer, "/api/session")
    assert json.loads(body)["human"] == {"address": "user", "name": "You", "kind": "human"}


def test_session_token_is_stable_for_one_server(viewer):
    first = json.loads(fetch(viewer, "/api/session")[2])
    assert json.loads(fetch(viewer, "/api/session")[2])["csrf_token"] == first["csrf_token"]


@pytest.mark.parametrize(("key", "value"), [
    ("X-One-Conv-Token", None), ("X-One-Conv-Token", "wrong"),
    ("Origin", None), ("Origin", "http://attacker.example"), ("Origin", "null"),
    ("Origin", "https://127.0.0.1"),
])
def test_message_requires_own_origin_and_token(viewer, human_headers, human_payload, key, value):
    if value is None:
        human_headers.pop(key)
    else:
        human_headers[key] = value
    assert post_message(viewer, human_headers, human_payload)[0] == 403


def test_refused_identity_does_not_claim_a_message(viewer, homes, human_headers, human_payload):
    human_headers.pop("X-One-Conv-Token")
    post_message(viewer, human_headers, human_payload)
    assert not (homes / "live" / "web-messages.sqlite3").exists()


def test_message_refuses_a_foreign_host(viewer, human_headers, human_payload):
    human_headers["Host"] = "attacker.example"
    assert post_message(viewer, human_headers, human_payload)[0] == 421


def test_json_does_not_allow_cross_origin_reads(viewer):
    _, headers, _ = fetch(viewer, "/api/session")
    assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_message_requires_json_content_type(viewer, human_headers, human_payload, content_type):
    human_headers["Content-Type"] = content_type
    assert post_message(viewer, human_headers, human_payload)[0] == 415


def test_encoded_bodies_are_refused(viewer, human_headers, human_payload):
    human_headers["Content-Encoding"] = "gzip"
    assert post_message(viewer, human_headers, human_payload)[0] == 415


@pytest.mark.parametrize("body", [b"{broken", b"\xff", b"[" * 1500])
def test_malformed_json_is_actionable(viewer, human_headers, body):
    assert post_message(viewer, human_headers, body=body)[0] == 400


def test_declared_body_limit_is_enforced(viewer, human_headers):
    assert post_message(viewer, human_headers, body=b"x" * (web_send.BODY_LIMIT + 1))[0] == 413


def test_streamed_body_limit_is_enforced(viewer, human_headers):
    chunks = iter([b"x" * 32768, b"y" * 32768, b"z"])
    assert post_message(viewer, human_headers, body=chunks, chunked=True)[0] == 413


@pytest.mark.parametrize(("key", "value"), [
    ("id", "INVALID"), ("id", "a" * 31), ("id", "A" * 32), ("text", "  "), ("text", 7),
    ("target", "release"), ("target", "@agent"), ("target", "#bad topic"), ("target", "#"),
    ("sender", "omp:agent"), ("from", "omp:agent"), ("origin", "unknown"),
])
def test_invalid_message_fields_are_refused(viewer, human_headers, human_payload, key, value):
    assert post_message(viewer, human_headers, {**human_payload, key: value})[0] == 400


@pytest.mark.parametrize("payload", [None, [], "hello", {}, {"target": "#release", "text": "hi"}])
def test_message_requires_exact_object_shape(homes, payload):
    with pytest.raises(web_send.SendError, match="only id, target and text"):
        web_send.validate(payload)


def test_text_limit_is_enforced(viewer, human_headers, human_payload):
    human_payload["text"] = "x" * (web_send.TEXT_LIMIT + 1)
    assert post_message(viewer, human_headers, human_payload)[0] == 413


def test_http_send_reaches_a_disposable_omp_inbox(viewer, homes, human_headers, human_payload):
    _omp(homes, "o1")
    post_message(viewer, human_headers, human_payload)
    rows = list((homes / "live" / "inbox" / "omp" / "o1").glob("*.json"))
    assert human_payload["text"] in json.loads(rows[0].read_text())["text"]


def test_http_send_is_human_even_when_launched_by_an_agent(viewer, homes, human_headers, human_payload, monkeypatch):
    _omp(homes, "o1")
    monkeypatch.setattr(router, "whoami", lambda *args, **kwargs: LiveSession("claude", "launcher"))
    assert post_message(viewer, human_headers, human_payload)[1]["receipt"]["from"] == "user"


def test_human_envelope_is_labelled(homes):
    assert 'from="user" kind="human"' in envelope("Review this.", None, "user")


def test_human_envelope_offers_a_direct_reply():
    assert 'to="user"' in envelope("Review this.", None, "user")


def test_human_topic_envelope_offers_a_topic_reply():
    assert 'to="#release"' in envelope("Review this.", None, "user", topic="release")


def test_remote_human_envelope_is_labelled():
    assert 'from="laptop/user" kind="human"' in envelope("Review this.", None, "laptop/user")


def test_remote_human_topic_envelope_offers_a_topic_reply():
    assert 'to="#release"' in envelope("Review this.", None, "laptop/user", topic="release")


def test_remote_human_envelope_does_not_offer_a_local_user_reply():
    assert 'to="user"' not in envelope("Review this.", None, "laptop/user")


def test_http_receipt_reports_transport_acceptance(viewer, homes, human_headers, human_payload):
    _omp(homes, "o1")
    assert post_message(viewer, human_headers, human_payload)[1]["status"] == "sent"


def test_repeated_http_send_reuses_the_first_receipt(viewer, homes, human_headers, human_payload):
    _omp(homes, "o1")
    first = post_message(viewer, human_headers, human_payload)
    assert post_message(viewer, human_headers, human_payload) == first


def test_repeated_http_send_does_not_make_a_second_inbox_file(viewer, homes, human_headers, human_payload):
    _omp(homes, "o1")
    post_message(viewer, human_headers, human_payload)
    post_message(viewer, human_headers, human_payload)
    assert len(list((homes / "live" / "inbox" / "omp" / "o1").glob("*.json"))) == 1


def test_repeated_send_is_logged_once(viewer, homes, human_headers, human_payload):
    _omp(homes, "o1")
    post_message(viewer, human_headers, human_payload)
    post_message(viewer, human_headers, human_payload)
    assert len(web.log()) == 1


def test_conflicting_http_id_is_refused(viewer, homes, human_headers, human_payload):
    _omp(homes, "o1")
    post_message(viewer, human_headers, human_payload)
    assert post_message(viewer, human_headers, {**human_payload, "text": "different"})[0] == 409


def test_crashed_claim_is_never_delivered(homes, human_payload):
    _omp(homes, "o1")
    web_send._claim(human_payload)
    web_send.send(human_payload)
    assert not (homes / "live" / "inbox").exists()


def test_crashed_claim_reports_unconfirmed(homes, human_payload):
    web_send._claim(human_payload)
    assert web_send.send(human_payload)[1]["status"] == "unconfirmed"


def test_concurrent_sends_have_only_one_claim(homes, human_payload):
    _omp(homes, "o1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(web_send.send, [human_payload, human_payload]))
    assert len(list((homes / "live" / "inbox" / "omp" / "o1").glob("*.json"))) == 1


@pytest.fixture
def failed_adapter(homes, monkeypatch):
    _omp(homes, "o1")

    def failed(session, text):
        raise RuntimeError("private diagnostic /do/not/expose")

    monkeypatch.setattr(router.ADAPTERS["omp"], "deliver", failed)


def test_failed_adapter_returns_unconfirmed(viewer, human_headers, human_payload, failed_adapter):
    assert post_message(viewer, human_headers, human_payload)[1]["status"] == "unconfirmed"


def test_failed_adapter_does_not_expose_internal_error(viewer, human_headers, human_payload, failed_adapter):
    assert "private diagnostic" not in json.dumps(post_message(viewer, human_headers, human_payload)[1])


def test_failed_adapter_detail_survives_privately(homes, human_payload, failed_adapter):
    web_send.send(human_payload)
    with sqlite3.connect(homes / "live" / "web-messages.sqlite3") as db:
        detail = db.execute("SELECT detail FROM messages").fetchone()[0]
    assert "private diagnostic" in detail


def test_failed_adapter_cannot_be_prompted_again(homes, human_payload, failed_adapter, monkeypatch):
    web_send.send(human_payload)
    calls = []
    monkeypatch.setattr(router.ADAPTERS["omp"], "deliver", lambda *args: calls.append(args))
    web_send.send(human_payload)
    assert calls == []


def test_adapter_accepting_then_raising_is_not_prompted_again(homes, human_payload, monkeypatch):
    _omp(homes, "o1")
    deliver = router.ADAPTERS["omp"].deliver

    def accepted_then_failed(session, text):
        deliver(session, text)
        raise OSError("lost acknowledgment")

    monkeypatch.setattr(router.ADAPTERS["omp"], "deliver", accepted_then_failed)
    web_send.send(human_payload)
    web_send.send(human_payload)
    assert len(list((homes / "live" / "inbox" / "omp" / "o1").glob("*.json"))) == 1


def test_unavailable_journal_reports_an_actionable_error(viewer, human_headers, human_payload, monkeypatch):
    def unavailable():
        raise sqlite3.OperationalError("private diagnostic")

    monkeypatch.setattr(web_send, "_database", unavailable)
    assert post_message(viewer, human_headers, human_payload)[0] == 503


def test_unavailable_journal_never_delivers(homes, human_payload, monkeypatch):
    _omp(homes, "o1")

    def unavailable():
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(web_send, "_database", unavailable)
    try:
        web_send.send(human_payload)
    except sqlite3.OperationalError:
        pass
    assert not (homes / "live" / "inbox").exists()


def test_failed_receipt_write_keeps_the_duplicate_claim(homes, human_payload, monkeypatch):
    _omp(homes, "o1")

    def unavailable(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(web_send, "_finish", unavailable)
    web_send.send(human_payload)
    web_send.send(human_payload)
    assert len(list((homes / "live" / "inbox" / "omp" / "o1").glob("*.json"))) == 1


def test_a_separate_process_replays_the_durable_claim(homes, human_payload):
    _omp(homes, "o1")
    web_send.send(human_payload)
    code = "from one_conv.live import web_send; import json; web_send.send(json.loads(" + repr(json.dumps(human_payload)) + "))"
    subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, check=True)
    assert len(list((homes / "live" / "inbox" / "omp" / "o1").glob("*.json"))) == 1


@pytest.mark.parametrize("outcome", ["pending", "unconfirmed", "refused: no_running_turn"])
def test_unknown_adapter_receipt_is_unconfirmed(homes, human_payload, monkeypatch, outcome):
    _omp(homes, "o1")
    monkeypatch.setattr(router.ADAPTERS["omp"], "deliver", lambda *args: outcome)
    assert web_send.send(human_payload)[1]["status"] == "unconfirmed"


def test_agent_direct_reply_to_human_is_visible(homes):
    router.send("user", "The release passed.", LiveSession("omp", "o1"))
    assert web.direct()["threads"][0]["messages"][0]["text"] == "The release passed."


def test_agent_direct_reply_to_human_preserves_agent_identity(homes):
    router.send("user", "The release passed.", LiveSession("omp", "o1"))
    assert web.log()[0]["from"] == "omp:o1"


def test_agent_reply_to_human_keeps_the_complete_result(homes):
    text = "a" * router.MESSAGE_LOG_TEXT + " THE FINAL RESULT"
    router.send("user", text, LiveSession("omp", "o1"))
    assert web.direct()["threads"][0]["messages"][0]["text"] == text


def test_outgoing_human_message_keeps_its_complete_text(homes):
    _omp(homes, "o1")
    text = "a" * router.MESSAGE_LOG_TEXT + " THE COMPLETE REQUEST"
    router.send("omp:o1", text, None, origin="user")
    assert web.direct()["threads"][0]["messages"][0]["text"] == text


def test_agent_diagnostic_copy_stays_bounded(homes):
    _omp(homes, "o1")
    router.send("omp:o1", "a" * (router.MESSAGE_LOG_TEXT + 1), LiveSession("claude", "c1"))
    assert len(web.log()[0]["text"]) == router.MESSAGE_LOG_TEXT


def test_agent_reply_to_human_never_resolves_a_harness(homes, monkeypatch):
    calls = []
    monkeypatch.setattr(router, "resolve", lambda *args, **kwargs: calls.append(args))
    router.send("user", "The release passed.", LiveSession("omp", "o1"))
    assert calls == []


def test_human_sink_storage_failure_is_reported(homes, monkeypatch):
    def failed(row):
        raise OSError("disk full")

    monkeypatch.setattr(router, "log_message", failed)
    with pytest.raises(OSError, match="disk full"):
        router.send("user", "The release passed.", LiveSession("omp", "o1"))


def test_human_topic_member_accepts_agent_reply(homes):
    team.join("release", "user")
    receipt = router.send("#release", "The release passed.", LiveSession("omp", "o1"))
    assert receipt["deliveries"][0]["status"] == "stored for human"


def test_http_topic_send_remains_peek_only_for_other_readers(viewer, homes, human_headers, human_payload):
    _omp(homes, "o1")
    team.join("release", "omp:o1")
    post_message(viewer, human_headers, {**human_payload, "target": "#release"})
    fetch(viewer, "/api/topics/release")
    assert team.catch_up("release", "omp:o1")["messages"][0]["text"] == human_payload["text"]
