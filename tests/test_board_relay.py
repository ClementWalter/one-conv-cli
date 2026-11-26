"""The board on the relay over real HTTP: topics, posts, reads after a cursor, tags, files, deletes, search, limits.

Every test runs against the Python relay, and against the Worker with ONE_CONV_TEST_RELAY_WORKER=1.
"""

import hashlib
import http.client
import json
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import pytest

from test_live_hub import call, hub, two_nodes  # noqa: F401  (fixtures)

pytest.importorskip("uvicorn")


def post(url, token, topic, text="a finding", sender="claude:aaa", **extra):
    """(status, body) of one post under a fresh id unless `id` is given."""
    return call(url, f"/v1/board/{topic}", token, "POST",
                {"id": uuid.uuid4().hex, "from": sender, "text": text, **extra})


def upload(url, token, topic, data: bytes, name="notes.txt", content_type="text/plain"):
    """(status, body) of one raw file upload."""
    request = Request(f"{url}/v1/board/{topic}/files", data=data, method="POST",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": content_type,
                               "X-File-Name": quote(name, safe="")})
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def download(url, token, topic, ident):
    """(status, body bytes) of one file download."""
    request = Request(f"{url}/v1/board/{topic}/files/{ident}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


@pytest.fixture
def board(two_nodes):
    """(url, mac token, box token) with nothing posted yet."""
    _, url, mac, box = two_nodes
    return url, mac, box


@pytest.fixture
def shared(board):
    """(url, mac, box, file metadata): mac posted `report.bin` to topic `release`."""
    url, mac, box = board
    meta = upload(url, mac, "release", b"\x00\x01binary\xff" * 100, "report.bin", "application/octet-stream")[1]
    post(url, mac, "release", "the report", files=[meta["id"]])
    return url, mac, box, meta


def test_the_first_post_creates_its_topic(board):
    url, mac, _ = board
    post(url, mac, "release")
    assert [t["topic"] for t in call(url, "/v1/board", mac)[1]["topics"]] == ["release"]


def test_a_topic_counts_its_posts(board):
    url, mac, _ = board
    post(url, mac, "release")
    post(url, mac, "release")
    assert call(url, "/v1/board", mac)[1]["topics"][0]["posts"] == 2


def test_topics_list_the_most_recently_posted_first(board):
    url, mac, _ = board
    post(url, mac, "older")
    post(url, mac, "newer")
    assert [t["topic"] for t in call(url, "/v1/board", mac)[1]["topics"]] == ["newer", "older"]


def test_a_post_can_name_its_topic(board):
    url, mac, _ = board
    post(url, mac, "release", title="Release 2.0")
    assert call(url, "/v1/board", mac)[1]["topics"][0]["title"] == "Release 2.0"


def test_unread_counts_posts_after_the_readers_position(board):
    url, mac, _ = board
    first = post(url, mac, "release")[1]
    post(url, mac, "release")
    assert call(url, f"/v1/board?read=release:{first['seq']}", mac)[1]["topics"][0]["unread"] == 1


def test_without_a_position_every_post_is_unread(board):
    url, mac, _ = board
    post(url, mac, "release")
    assert call(url, "/v1/board", mac)[1]["topics"][0]["unread"] == 1


def test_a_malformed_read_position_is_refused(board):
    url, mac, _ = board
    assert call(url, "/v1/board?read=release", mac)[0] == 400


def test_another_node_reads_a_post(board):
    url, mac, box = board
    post(url, mac, "release", "ship it on monday")
    assert [p["text"] for p in call(url, "/v1/board/release", box)[1]["posts"]] == ["ship it on monday"]


def test_a_post_names_its_node_and_author(board):
    url, mac, box = board
    post(url, mac, "release", name="@lead")
    found = call(url, "/v1/board/release", box)[1]["posts"][0]
    assert (found["from"], found["name"]) == ("mac/claude:aaa", "@lead")


def test_reading_after_a_cursor_returns_only_later_posts(board):
    url, mac, _ = board
    first = post(url, mac, "release", "one")[1]
    post(url, mac, "release", "two")
    assert [p["text"] for p in call(url, f"/v1/board/release?after={first['seq']}", mac)[1]["posts"]] == ["two"]


def test_a_read_is_limited(board):
    url, mac, _ = board
    for text in ("one", "two", "three"):
        post(url, mac, "release", text)
    assert [p["text"] for p in call(url, "/v1/board/release?limit=2", mac)[1]["posts"]] == ["one", "two"]


def test_reading_returns_the_topics_newest_seq(board):
    url, mac, _ = board
    post(url, mac, "release")
    newest = post(url, mac, "release")[1]
    assert call(url, "/v1/board/release?limit=1", mac)[1]["last"] == newest["seq"]


def test_posts_of_another_topic_are_not_read(board):
    url, mac, _ = board
    post(url, mac, "release", "for release")
    post(url, mac, "infra", "for infra")
    assert [p["text"] for p in call(url, "/v1/board/release", mac)[1]["posts"]] == ["for release"]


def test_an_unknown_topic_is_not_found(board):
    url, mac, _ = board
    assert call(url, "/v1/board/nowhere", mac)[0] == 404


def test_a_tag_filter_keeps_posts_carrying_it(board):
    url, mac, _ = board
    post(url, mac, "release", "a decision", tags=["decision"])
    post(url, mac, "release", "a finding", tags=["finding"])
    assert [p["text"] for p in call(url, "/v1/board/release?tag=decision", mac)[1]["posts"]] == ["a decision"]


def test_a_read_filtering_more_than_eight_tags_is_refused(board):
    url, mac, _ = board
    post(url, mac, "release")
    assert call(url, "/v1/board/release?" + "&".join(f"tag=t{i}" for i in range(9)), mac)[0] == 400


def test_a_post_keeps_its_tags_once_each(board):
    url, mac, _ = board
    post(url, mac, "release", tags=["decision", "decision", "infra"])
    assert call(url, "/v1/board/release", mac)[1]["posts"][0]["tags"] == ["decision", "infra"]


def test_a_post_returns_the_previous_seq_of_its_topic(board):
    url, mac, _ = board
    first = post(url, mac, "release")[1]
    assert post(url, mac, "release")[1]["previous"] == first["seq"]


@pytest.mark.parametrize("topic", ["Release", "-release", "a" * 49, "search", "re_lease"])
def test_a_malformed_topic_is_refused(board, topic):
    url, mac, _ = board
    assert post(url, mac, topic)[0] in (400, 404, 405)


@pytest.mark.parametrize("extra, status", [
    ({"text": "x" * (32 * 1024 + 1)}, 413),
    ({"text": ""}, 400),
    ({"tags": [f"t{i}" for i in range(9)]}, 400),
    ({"tags": ["Decision"]}, 400),
    ({"name": "n" * 201}, 400),
    ({"title": "t" * 201}, 400),
    ({"id": "not-hex"}, 400),
    ({"files": ["0" * 32]}, 400),
], ids=["text-over-32kb", "empty-text", "nine-tags", "upper-case-tag", "long-name", "long-title", "bad-id", "unknown-file"])
def test_a_post_outside_the_limits_is_refused(board, extra, status):
    url, mac, _ = board
    assert post(url, mac, "release", **extra)[0] == status


def test_a_post_of_exactly_32kb_is_stored(board):
    url, mac, _ = board
    assert post(url, mac, "release", "é" * (16 * 1024))[0] == 200


def test_a_sender_this_node_did_not_publish_is_refused(board):
    url, mac, _ = board
    assert post(url, mac, "release", sender="claude:bbb")[0] == 403


def test_the_person_at_a_terminal_may_post(board):
    url, mac, _ = board
    assert post(url, mac, "release", sender="user")[0] == 200


def test_a_retry_under_the_same_id_is_a_duplicate(board):
    url, mac, _ = board
    ident = uuid.uuid4().hex
    post(url, mac, "release", "once", id=ident)
    assert post(url, mac, "release", "once", id=ident)[1]["duplicate"] is True


def test_a_retry_under_the_same_id_is_stored_once(board):
    url, mac, _ = board
    ident = uuid.uuid4().hex
    post(url, mac, "release", "once", id=ident)
    post(url, mac, "release", "once", id=ident)
    assert len(call(url, "/v1/board/release", mac)[1]["posts"]) == 1


def test_a_different_post_under_a_used_id_is_a_conflict(board):
    url, mac, _ = board
    ident = uuid.uuid4().hex
    post(url, mac, "release", "once", id=ident)
    assert post(url, mac, "release", "twice", id=ident)[0] == 409


def test_another_nodes_post_under_a_used_id_is_a_conflict(board):
    url, mac, box = board
    ident = uuid.uuid4().hex
    post(url, mac, "release", "once", id=ident)
    assert post(url, box, "release", "once", id=ident, sender="claude:bbb")[0] == 409


def test_a_retry_that_uploads_the_same_file_again_is_a_duplicate(board):
    url, mac, _ = board
    ident = uuid.uuid4().hex
    first = upload(url, mac, "release", b"same bytes")[1]
    post(url, mac, "release", "with a file", id=ident, files=[first["id"]])
    again = upload(url, mac, "release", b"same bytes")[1]
    assert post(url, mac, "release", "with a file", id=ident, files=[again["id"]])[1]["duplicate"] is True


def test_an_upload_reports_the_files_sha256(board):
    url, mac, _ = board
    assert upload(url, mac, "release", b"hello board")[1]["sha256"] == hashlib.sha256(b"hello board").hexdigest()


def test_a_file_downloads_byte_identical(shared):
    url, _, box, meta = shared
    assert download(url, box, "release", meta["id"])[1] == b"\x00\x01binary\xff" * 100


def test_a_post_lists_its_file_with_name_size_type_and_sha256(shared):
    url, _, box, meta = shared
    listed = call(url, "/v1/board/release", box)[1]["posts"][0]["files"][0]
    assert listed == {"id": meta["id"], "name": "report.bin", "size": 900,
                      "content_type": "application/octet-stream", "sha256": hashlib.sha256(b"\x00\x01binary\xff" * 100).hexdigest()}


def test_a_file_name_keeps_its_unicode(board):
    url, mac, _ = board
    assert upload(url, mac, "release", b"x", "résumé final.txt")[1]["name"] == "résumé final.txt"


def test_a_file_name_with_a_slash_is_refused(board):
    url, mac, _ = board
    assert upload(url, mac, "release", b"x", "../etc/passwd")[0] == 400


def test_an_empty_file_is_refused(board):
    url, mac, _ = board
    assert upload(url, mac, "release", b"")[0] == 413


def test_a_file_declared_over_25mb_is_refused(board):
    url, mac, _ = board
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        connection.request("POST", "/v1/board/release/files", body=b"x",
                           headers={"Authorization": f"Bearer {mac}", "X-File-Name": "big.bin",
                                    "Content-Length": str(25_000_000 + 1)})
        status = connection.getresponse().status
    finally:
        connection.close()
    assert status == 413


def test_a_post_carries_at_most_four_files(board):
    url, mac, _ = board
    files = [upload(url, mac, "release", bytes([i]))[1]["id"] for i in range(5)]
    assert post(url, mac, "release", files=files)[0] == 400


def test_a_file_uploaded_to_another_topic_is_refused(board):
    url, mac, _ = board
    meta = upload(url, mac, "infra", b"x")[1]
    assert post(url, mac, "release", files=[meta["id"]])[0] == 400


def test_a_file_another_node_uploaded_is_refused(board):
    url, mac, box = board
    meta = upload(url, box, "release", b"x")[1]
    assert post(url, mac, "release", files=[meta["id"]])[0] == 400


def test_a_file_already_attached_elsewhere_is_refused(shared):
    url, mac, _, meta = shared
    assert post(url, mac, "release", "again", files=[meta["id"]])[0] == 400


def test_a_file_no_post_carries_is_not_served(board):
    url, mac, _ = board
    meta = upload(url, mac, "release", b"x")[1]
    assert download(url, mac, "release", meta["id"])[0] == 404


def test_a_file_is_served_only_under_its_topic(shared):
    url, _, box, meta = shared
    post(url, box, "infra", sender="claude:bbb")
    assert download(url, box, "infra", meta["id"])[0] == 404


def test_a_post_with_a_file_needs_no_text(board):
    url, mac, _ = board
    meta = upload(url, mac, "release", b"x")[1]
    assert post(url, mac, "release", "", files=[meta["id"]])[0] == 200


def test_the_posting_node_deletes_its_post(board):
    url, mac, _ = board
    found = post(url, mac, "release")[1]
    assert call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")[0] == 200


def test_a_deleted_post_is_not_read(board):
    url, mac, _ = board
    found = post(url, mac, "release", "gone")[1]
    post(url, mac, "release", "kept")
    call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")
    assert [p["text"] for p in call(url, "/v1/board/release", mac)[1]["posts"]] == ["kept"]


def test_a_deleted_post_leaves_the_topic_count(board):
    url, mac, _ = board
    found = post(url, mac, "release")[1]
    post(url, mac, "release")
    call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")
    assert call(url, "/v1/board", mac)[1]["topics"][0]["posts"] == 1


def test_deleting_the_last_post_drops_the_topic_from_the_listing(board):
    url, mac, _ = board
    found = post(url, mac, "release")[1]
    post(url, mac, "infra")
    call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")
    assert [t["topic"] for t in call(url, "/v1/board", mac)[1]["topics"]] == ["infra"]


def test_posting_to_an_emptied_topic_lists_it_again(board):
    url, mac, _ = board
    found = post(url, mac, "release")[1]
    call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")
    post(url, mac, "release", "back")
    assert [(t["topic"], t["posts"]) for t in call(url, "/v1/board", mac)[1]["topics"]] == [("release", 1)]


def test_deleting_twice_counts_once(board):
    url, mac, _ = board
    found = post(url, mac, "release")[1]
    post(url, mac, "release")
    call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")
    call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")
    assert call(url, "/v1/board", mac)[1]["topics"][0]["posts"] == 1


def test_another_node_cannot_delete_a_post(board):
    url, mac, box = board
    found = post(url, mac, "release")[1]
    assert call(url, f"/v1/board/release/posts/{found['id']}", box, "DELETE")[0] == 403


def test_deleting_an_unknown_post_is_not_found(board):
    url, mac, _ = board
    post(url, mac, "release")
    assert call(url, f"/v1/board/release/posts/{'0' * 32}", mac, "DELETE")[0] == 404


def test_a_deleted_posts_file_is_not_served(shared):
    url, mac, box, meta = shared
    ident = call(url, "/v1/board/release", mac)[1]["posts"][0]["id"]
    call(url, f"/v1/board/release/posts/{ident}", mac, "DELETE")
    assert download(url, box, "release", meta["id"])[0] == 404


def test_search_finds_a_substring_ignoring_case(board):
    url, mac, _ = board
    post(url, mac, "release", "The Migration needs a lock")
    post(url, mac, "release", "unrelated")
    assert [p["text"] for p in call(url, "/v1/board/search?q=migration", mac)[1]["posts"]] == ["The Migration needs a lock"]


def test_search_returns_the_newest_first(board):
    url, mac, _ = board
    post(url, mac, "release", "lock one")
    post(url, mac, "release", "lock two")
    assert [p["text"] for p in call(url, "/v1/board/search?q=lock", mac)[1]["posts"]] == ["lock two", "lock one"]


def test_search_names_each_hits_topic(board):
    url, mac, _ = board
    post(url, mac, "infra", "lock")
    assert call(url, "/v1/board/search?q=lock", mac)[1]["posts"][0]["topic"] == "infra"


def test_a_phrase_search_needs_the_words_together(board):
    url, mac, _ = board
    post(url, mac, "release", "lock the migration")
    assert call(url, "/v1/board/search?q=migration+lock", mac)[1]["posts"] == []


def test_an_all_words_search_matches_them_in_any_order(board):
    url, mac, _ = board
    post(url, mac, "release", "lock the migration")
    assert len(call(url, "/v1/board/search?q=migration+lock&words=1", mac)[1]["posts"]) == 1


def test_search_keeps_to_one_topic(board):
    url, mac, _ = board
    post(url, mac, "release", "lock in release")
    post(url, mac, "infra", "lock in infra")
    assert [p["text"] for p in call(url, "/v1/board/search?q=lock&topic=infra", mac)[1]["posts"]] == ["lock in infra"]


def test_search_takes_a_percent_sign_literally(board):
    url, mac, _ = board
    post(url, mac, "release", "coverage is 80 percent")
    assert call(url, "/v1/board/search?q=%25", mac)[1]["posts"] == []


def test_search_skips_deleted_posts(board):
    url, mac, _ = board
    found = post(url, mac, "release", "lock")[1]
    call(url, f"/v1/board/release/posts/{found['id']}", mac, "DELETE")
    assert call(url, "/v1/board/search?q=lock", mac)[1]["posts"] == []


def test_an_empty_search_is_refused(board):
    url, mac, _ = board
    assert call(url, "/v1/board/search?q=", mac)[0] == 400


def test_the_board_needs_a_node_token(board):
    url, _, _ = board
    assert call(url, "/v1/board", None)[0] == 401
