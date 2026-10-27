#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
"""Images attached to a message: stored on the hub, carried by the post, fetched by the bridge, shown in the workspace."""

import json
from pathlib import Path

import pytest

from one_conv.control.store import ControlStore, validate_message
from one_conv.live import bridge
from one_conv.live.hub import ATTACHMENT_LIMIT, Hub, HubError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
NODE = "workspace"


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.sqlite3"))
    state.add_token(NODE)
    state.publish(NODE, [{"address": "user"}])
    state.publish("mac", [{"address": "claude:c1"}])
    state.add_token("mac")
    return state


@pytest.fixture
def stored(hub):
    return hub.store_attachment(NODE, "image/png", PNG)


def message(stored, **extra):
    return {"id": "a" * 32, "from": "user", "to": "mac/claude:c1", "text": "See the screenshot.",
            "attachments": [stored["id"]], **extra}


# --- storing -----------------------------------------------------------------------------------------


def test_stored_image_gets_a_32_hex_id(stored):
    assert len(stored["id"]) == 32 and int(stored["id"], 16) >= 0


def test_stored_image_keeps_its_type_and_size(stored):
    assert (stored["media_type"], stored["size"]) == ("image/png", len(PNG))


def test_stored_image_can_be_fetched_back(hub, stored):
    assert hub.attachment(stored["id"]) == ("image/png", PNG)


def test_only_images_are_stored(hub):
    with pytest.raises(HubError, match="PNG, JPEG, GIF or WebP"):
        hub.store_attachment(NODE, "application/pdf", b"%PDF")


def test_an_oversized_image_is_refused(hub):
    with pytest.raises(HubError, match="at most"):
        hub.store_attachment(NODE, "image/png", b"\x00" * (ATTACHMENT_LIMIT + 1))


def test_an_unknown_attachment_is_not_found(hub):
    with pytest.raises(HubError, match="no such attachment"):
        hub.attachment("f" * 32)


# --- sending -----------------------------------------------------------------------------------------


def test_a_message_may_carry_an_image(hub, stored):
    assert hub.send(NODE, message(stored))["recipients"][0]["to"] == "mac/claude:c1"


def test_a_message_may_be_an_image_alone(hub, stored):
    assert hub.send(NODE, message(stored, text=""))["duplicate"] is False


def test_a_message_without_text_or_image_is_refused(hub):
    with pytest.raises(HubError, match="non-empty unless an image is attached"):
        hub.send(NODE, {"id": "a" * 32, "from": "user", "to": "mac/claude:c1", "text": ""})


def test_an_unuploaded_attachment_refuses_the_message(hub, stored):
    with pytest.raises(HubError, match="was not uploaded"):
        hub.send(NODE, message(stored, attachments=["0" * 32]))


def test_too_many_attachments_are_refused(hub, stored):
    with pytest.raises(HubError, match="at most 4"):
        hub.send(NODE, message(stored, attachments=[stored["id"]] * 5))


def test_the_delivery_carries_the_image(hub, stored):
    hub.send(NODE, message(stored))
    assert hub.inbox("mac", 0)[0]["attachments"] == [{"id": stored["id"], "media_type": "image/png", "size": len(PNG)}]


def test_a_delivery_without_images_has_no_attachments_key(hub):
    hub.send(NODE, {"id": "a" * 32, "from": "user", "to": "mac/claude:c1", "text": "Plain."})
    assert "attachments" not in hub.inbox("mac", 0)[0]


def test_channel_history_carries_the_image(hub, stored):
    hub.join(NODE, "release", "user")
    hub.send(NODE, message(stored, to="#release"))
    assert hub.history("release")[0]["attachments"][0]["id"] == stored["id"]


def test_a_retry_with_other_images_is_a_conflict(hub, stored):
    hub.send(NODE, message(stored))
    with pytest.raises(HubError, match="different message"):
        hub.send(NODE, message(stored, attachments=[]))


# --- workspace -------------------------------------------------------------------------------------


def test_browser_payload_may_name_attachments():
    assert validate_message({"id": "a" * 32, "target": "#release", "text": "", "attachments": ["b" * 32]})["attachments"] == ["b" * 32]


def test_browser_payload_needs_text_or_an_image():
    with pytest.raises(HubError, match="attach an image"):
        validate_message({"id": "a" * 32, "target": "#release", "text": " "})


def test_browser_payload_attachments_must_be_ids():
    with pytest.raises(HubError, match="uploaded image ids"):
        validate_message({"id": "a" * 32, "target": "#release", "text": "x", "attachments": ["nope"]})


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path / "workspace")
    store.hub.add_token("mac")
    store.hub.publish("mac", [{"address": "claude:c1"}])
    return store


def test_workspace_message_shows_its_image(store):
    image = store.store_attachment("image/png", PNG)
    store.send({"id": "a" * 32, "target": "mac/claude:c1", "text": "", "attachments": [image["id"]]})
    assert store.direct()["threads"][0]["messages"][0]["attachments"][0]["id"] == image["id"]


def test_workspace_channel_post_shows_its_image(store):
    image = store.store_attachment("image/png", PNG)
    store.send({"id": "a" * 32, "target": "#general", "text": "Look.", "attachments": [image["id"]]})
    assert store.topic("general")["messages"][-1]["attachments"][0]["media_type"] == "image/png"


# --- bridge ------------------------------------------------------------------------------------------


def test_delivery_text_names_where_each_image_was_saved():
    attachments = [{"id": "a" * 32, "media_type": "image/png", "size": 7}]
    text = bridge.with_attachments("Look at this.", attachments, lambda a: Path("/live/attachments") / (a["id"] + ".png"))
    assert text == ("Look at this.\n\n[Attached image 1: /live/attachments/" + "a" * 32
                    + ".png (image/png, 7 bytes). Read this file to view it.]")


def test_delivery_text_names_an_image_the_hub_would_not_hand_over():
    def refuse(_attachment):
        raise OSError("gone")
    text = bridge.with_attachments("Look.", [{"id": "a" * 32}], refuse)
    assert "[Attached image 1 could not be fetched from the hub: OSError: gone]" in text


def test_delivery_text_is_unchanged_without_images():
    assert bridge.with_attachments("Plain.", None, lambda a: "x") == "Plain."


class DownloadingClient:
    def __init__(self):
        self.paths = []

    def download(self, path):
        self.paths.append(path)
        return "image/png", PNG


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ONE_CONV_LIVE_DIR", str(tmp_path / "live"))
    return tmp_path / "live"


def test_bridge_saves_an_image_under_its_live_directory(home, tmp_path):
    node = bridge.Bridge(DownloadingClient(), "mac", bridge.Ingress(tmp_path / "ingress.db"))
    path = node.fetch_attachment({"id": "a" * 32, "media_type": "image/png", "size": len(PNG)})
    assert path == home / "attachments" / ("a" * 32 + ".png") and path.read_bytes() == PNG


def test_bridge_fetches_each_image_once(home, tmp_path):
    client = DownloadingClient()
    node = bridge.Bridge(client, "mac", bridge.Ingress(tmp_path / "ingress.db"))
    for _ in range(2):
        node.fetch_attachment({"id": "a" * 32, "media_type": "image/png", "size": len(PNG)})
    assert client.paths == ["/v1/attachments/" + "a" * 32]


def test_bridge_keeps_a_saved_image_private(home, tmp_path):
    node = bridge.Bridge(DownloadingClient(), "mac", bridge.Ingress(tmp_path / "ingress.db"))
    path = node.fetch_attachment({"id": "a" * 32, "media_type": "image/png", "size": len(PNG)})
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_bridge_refuses_a_malformed_attachment_id(home, tmp_path):
    node = bridge.Bridge(DownloadingClient(), "mac", bridge.Ingress(tmp_path / "ingress.db"))
    with pytest.raises(ValueError, match="not an attachment id"):
        node.fetch_attachment({"id": "../etc/passwd", "media_type": "image/png"})


def test_inbox_item_json_round_trips_attachments(hub, stored):
    hub.send(NODE, message(stored))
    item = json.loads(json.dumps(hub.inbox("mac", 0)[0]))
    assert bridge.with_attachments(item["text"], item["attachments"], lambda a: "/p/" + a["id"]).endswith("Read this file to view it.]")
