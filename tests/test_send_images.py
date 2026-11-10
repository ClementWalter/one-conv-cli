#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Images sent with a message: checked before sending, uploaded to chats, inline in Cowork, copied for local agents."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from one_conv import images
from one_conv.live import router
from one_conv.live.model import LiveSession
from one_conv.providers.anthropic import AnthropicProvider
from one_conv.providers.base import JsonTransport
from one_conv.providers.cowork import CoworkProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
ORG = "11111111-2222-4333-8444-555555555555"
CHAT = "66666666-7777-4888-9999-aaaaaaaaaaaa"


@pytest.mark.parametrize("data,expected", [
    (PNG, "image/png"),
    (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
    (b"GIF89a....", "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"%PDF-1.7", None),
])
def test_media_type_comes_from_the_bytes(data, expected):
    assert images.media_type(data) == expected


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(PNG)
    return path


def test_load_reads_the_image(png):
    assert images.load([png])[0].media_type == "image/png"


def test_load_refuses_a_missing_file(tmp_path):
    with pytest.raises(images.ImageError, match="no image"):
        images.load([tmp_path / "gone.png"])


def test_load_refuses_a_disguised_file(tmp_path):
    fake = tmp_path / "fake.png"
    fake.write_text("not an image")
    with pytest.raises(images.ImageError, match="not a PNG"):
        images.load([fake])


def test_load_refuses_an_oversized_file(tmp_path):
    big = tmp_path / "big.png"
    with big.open("wb") as handle:
        handle.truncate(images.MAX_BYTES + 1)
    with pytest.raises(images.ImageError, match="at most"):
        images.load([big])


def test_load_refuses_too_many(png):
    with pytest.raises(images.ImageError, match="at most 4"):
        images.load([png] * 5)


def test_saved_lines_name_a_readable_copy(png, tmp_path):
    line = images.saved_lines(images.load([png]), tmp_path / "attachments")
    copy = line.split("Attached image 1: ")[1].split(" (")[0]
    assert open(copy, "rb").read() == PNG


class ChatTransport:
    """Serve a chat tree, accept uploads, and stream a short reply."""

    def __init__(self):
        self.uploads, self.posts = [], []

    def get(self, path, params=None):
        return {"uuid": CHAT, "current_leaf_message_uuid": None, "chat_messages": []}

    def post_upload(self, path, name, media_type, data):
        self.uploads.append((path, name, media_type))
        return {"file_uuid": f"file-{len(self.uploads)}"}

    def post_completion(self, path, body):
        self.posts.append(body)
        yield from [{"type": "message_start", "message": {"uuid": "r", "parent_uuid": "s"}}, {"type": "message_stop"}]


def test_chat_send_uploads_to_the_org(png):
    transport = ChatTransport()
    AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hi", images=images.load([png]))
    assert transport.uploads[0][0] == f"/api/{ORG}/upload"


def test_chat_send_attaches_the_uploaded_files(png):
    transport = ChatTransport()
    AnthropicProvider(None, ORG, transport=transport).send_message(CHAT, "hi", images=images.load([png, png]))
    assert transport.posts[0]["files"] == ["file-1", "file-2"]


class CoworkTransport:
    def __init__(self):
        self.posts = []

    def post_session_events(self, path, body):
        self.posts.append(body)
        return {"results": [{"event_id": "posted", "sequence_num": "1"}]}

    def get(self, path, params=None):
        return {"data": [{"event_id": "e2", "sequence_num": "2", "event_type": "result",
                          "payload": {"subtype": "success"}}], "next_cursor": None}


def test_cowork_send_puts_the_image_before_the_text(png):
    transport = CoworkTransport()
    provider = CoworkProvider.__new__(CoworkProvider)
    provider.transport = transport
    provider.send_message("cse_1", "hi", images=images.load([png]), sleep=lambda _: None)
    content = transport.posts[0]["events"][0]["payload"]["message"]["content"]
    assert [block["type"] for block in content] == ["image", "text"]


class UploadSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        return SimpleNamespace(status_code=503, headers={}, json=lambda: {})


def test_upload_route_is_the_only_upload():
    with pytest.raises(ValueError):
        JsonTransport(UploadSession(), "https://claude.ai", "fixture").post_upload("/api/x/files", "a", "image/png", PNG)


def test_upload_is_never_retried():
    session = UploadSession()
    with pytest.raises(Exception):
        JsonTransport(session, "https://claude.ai", "fixture").post_upload(f"/api/{ORG}/upload", "a", "image/png", PNG)
    assert len(session.calls) == 1


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    """Route cloud sends to a stub, recording the images it was given."""
    from one_conv import cloud_send
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    (tmp_path / "live").mkdir()
    given = []

    def deliver(product, query, wrapped, accounts=None, images=()):
        given.append(images)
        return {"to": "claude-chat:" + CHAT, "title": "t", "account": "a", "account_id": "a",
                "chat_message_id": "s", "reply_id": "r", "stop_reason": "end_turn", "reply": "red", "url": "u"}
    monkeypatch.setattr(cloud_send, "deliver", deliver)
    return given


SENDER = LiveSession("claude", "abc")


def test_router_hands_images_to_the_cloud(cloud, png):
    router.send("claude-chat:x", "what colour?", SENDER, images=[str(png)])
    assert cloud[0][0].media_type == "image/png"


def test_router_refuses_images_to_a_topic(cloud, png):
    with pytest.raises(router.LiveError, match="#topic"):
        router.send("#team", "look", SENDER, images=[str(png)])


def test_router_refuses_images_to_another_machine(cloud, png, monkeypatch):
    monkeypatch.setattr(router.hubclient, "configured", lambda: SimpleNamespace(node="macbook"))
    with pytest.raises(router.LiveError, match="another machine"):
        router.send("devbox/claude:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03", "look", SENDER, images=[str(png)])


def test_router_checks_images_before_sending(cloud, tmp_path):
    with pytest.raises(router.LiveError, match="no image"):
        router.send("claude-chat:x", "look", SENDER, images=[str(tmp_path / "gone.png")])
