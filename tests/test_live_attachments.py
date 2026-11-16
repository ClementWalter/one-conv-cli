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
"""Images attached to a message: uploaded to the relay, carried by the post, fetched by the receiving bridge."""

import json
import re
from pathlib import Path

import pytest

from one_conv.live import bridge
from one_conv.live.hub import ATTACHMENT_LIMIT, Hub, HubError, application

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
NODE = "laptop"


@pytest.fixture
def hub(tmp_path):
    state = Hub(str(tmp_path / "hub.sqlite3"))
    state.add_token(NODE)
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
    assert re.fullmatch(r"[0-9a-f]{32}", stored["id"])


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


def test_a_retry_with_other_images_is_a_conflict(hub, stored):
    hub.send(NODE, message(stored))
    with pytest.raises(HubError, match="different message"):
        hub.send(NODE, message(stored, attachments=[]))


# --- HTTP upload -------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from starlette.testclient import TestClient
    state = Hub(str(tmp_path / "relay.sqlite3"))
    token = state.add_token(NODE)
    with TestClient(application(state)) as http:
        yield http, {"Authorization": f"Bearer {token}"}


def test_an_uploaded_image_is_served_back_by_its_id(client):
    http, auth = client
    ident = http.post("/v1/attachments", content=PNG, headers={**auth, "Content-Type": "image/png"}).json()["id"]
    assert http.get(f"/v1/attachments/{ident}", headers=auth).content == PNG


def test_an_upload_without_a_token_is_refused(client):
    http, _ = client
    assert http.post("/v1/attachments", content=PNG, headers={"Content-Type": "image/png"}).status_code == 401


def test_an_upload_that_is_not_an_image_is_refused(client):
    http, auth = client
    response = http.post("/v1/attachments", content=b"%PDF", headers={**auth, "Content-Type": "application/pdf"})
    assert response.status_code == 415


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
    assert path == home / "attachments" / ("a" * 32 + ".png")


def test_bridge_saves_the_image_bytes(home, tmp_path):
    node = bridge.Bridge(DownloadingClient(), "mac", bridge.Ingress(tmp_path / "ingress.db"))
    path = node.fetch_attachment({"id": "a" * 32, "media_type": "image/png", "size": len(PNG)})
    assert path.read_bytes() == PNG


def test_bridge_fetches_each_image_once(home, tmp_path):
    client = DownloadingClient()
    node = bridge.Bridge(client, "mac", bridge.Ingress(tmp_path / "ingress.db"))
    node.fetch_attachment({"id": "a" * 32, "media_type": "image/png", "size": len(PNG)})
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
