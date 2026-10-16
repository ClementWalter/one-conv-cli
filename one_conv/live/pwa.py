"""Public installable app assets; private workspace responses are never cached."""

from pathlib import Path

_ROOT = Path(__file__).with_name("web_assets")
ASSETS = {
    "/manifest.webmanifest": ((_ROOT / "manifest.webmanifest").read_bytes(), "application/manifest+json"),
    "/sw.js": ((_ROOT / "sw.js").read_bytes(), "text/javascript"),
    "/offline.html": ((_ROOT / "offline.html").read_bytes(), "text/html"),
    **{f"/icons/{name}": ((_ROOT / "icons" / name).read_bytes(), "image/png")
       for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png")},
}
