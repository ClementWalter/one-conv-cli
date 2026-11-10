"""Images attached to a sent message: read once, typed by their bytes, bounded like hub attachments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import uuid

# The formats every receiver (Claude chats, Cowork, agents reading a file) can view.
MEDIA_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MAX_BYTES = 10_000_000
MAX_IMAGES = 4


class ImageError(ValueError):
    """An attached image is missing, too large, not an image, or one too many."""


@dataclass(frozen=True)
class Image:
    path: Path
    name: str
    media_type: str
    data: bytes


def media_type(data: bytes) -> str | None:
    """The image type from the file's leading bytes; the extension can lie."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def load(paths) -> tuple[Image, ...]:
    """Read and check every image before anything is sent, so a bad one sends nothing."""
    paths = list(paths or ())
    if len(paths) > MAX_IMAGES:
        raise ImageError(f"at most {MAX_IMAGES} images per message, got {len(paths)}")
    images = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        try:
            size = path.stat().st_size
        except OSError:
            raise ImageError(f"no image at {raw}") from None
        if size > MAX_BYTES:
            raise ImageError(f"{raw} is {size} bytes; an image may be at most {MAX_BYTES}")
        data = path.read_bytes()
        kind = media_type(data)
        if kind is None:
            raise ImageError(f"{raw} is not a PNG, JPEG, GIF or WebP image")
        images.append(Image(path, path.name, kind, data))
    return tuple(images)


def saved_lines(images, directory: Path) -> str:
    """Copy each image where agents on this machine keep attachments, and name the copies.

    The wording matches what the bridge writes for images that arrive through the hub.
    """
    if not images:
        return ""
    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    for number, image in enumerate(images, 1):
        copy = directory / f"{uuid.uuid4().hex}.{MEDIA_TYPES[image.media_type]}"
        # A copy outlives the sender's temporary files, so the receiver can read it later.
        shutil.copyfile(image.path, copy)
        lines.append(f"[Attached image {number}: {copy} ({image.media_type}, {len(image.data)} bytes). "
                     "Read this file to view it.]")
    return "\n\n" + "\n".join(lines)
