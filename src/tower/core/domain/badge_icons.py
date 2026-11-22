"""Organisation icons taken from their official website (pure rules, stdlib only).

An icon is looked for in the home page (``<link rel="apple-touch-icon">`` first, then
``<link rel="icon">``, largest declared size first), then at ``/favicon.ico``. Only
raster formats recognised from their first bytes are accepted (PNG, ICO, JPEG, GIF,
WebP): SVG can carry scripts and is never stored nor served.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_ICON_BYTES = 200_000
MIN_ICON_SIDE = 16
ICON_TYPES = {
    "image/png": "png",
    "image/x-icon": "ico",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
"""Stored content type → file extension of the public URL."""
FETCH_TYPES = frozenset({*ICON_TYPES, "image/vnd.microsoft.icon"})
"""Content types a server may announce for an accepted icon."""


@dataclass(frozen=True, slots=True)
class IconImage:
    content_type: str
    width: int
    height: int


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[int, int, str]] = []  # (rank, size, href)
        self._order = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rels = set(a.get("rel", "").lower().split())
        href = a.get("href", "").strip()
        if not href or "mask-icon" in rels or a.get("type", "").lower() == "image/svg+xml":
            return
        if href.lower().split("?", 1)[0].endswith(".svg"):
            return
        if "apple-touch-icon" in rels or "apple-touch-icon-precomposed" in rels:
            rank = 0
        elif "icon" in rels:
            rank = 1
        else:
            return
        size = 0
        for token in a.get("sizes", "").lower().split():
            w, _, h = token.partition("x")
            if w.isdigit() and h.isdigit():
                size = max(size, min(int(w), int(h)))
        self._order += 1
        self.links.append((rank, size, href))


def icon_candidates(html: str, page_url: str, *, limit: int = 4) -> list[str]:
    """Absolute http(s) URLs to try, best first, ending with ``/favicon.ico``."""
    parser = _LinkParser()
    try:
        parser.feed(html[:500_000])
    except Exception:
        parser.links = []
    ordered = sorted(parser.links, key=lambda x: (x[0], -x[1]))
    out: list[str] = []
    for _, _, href in ordered:
        url = urljoin(page_url, href)
        if urlsplit(url).scheme in ("http", "https") and url not in out:
            out.append(url)
    root = urljoin(page_url, "/favicon.ico")
    if root not in out:
        out.append(root)
    return out[:limit] if len(out) > limit else out


def sniff_icon(data: bytes) -> IconImage | None:
    """Content type and size of an accepted raster icon, from its first bytes; None
    when the format is unknown, the file too large or the image smaller than
    ``MIN_ICON_SIDE`` (dimensions unknown for JPEG and WebP: accepted as is)."""
    if not data or len(data) > MAX_ICON_BYTES:
        return None
    image = _sniff(data)
    if image is None:
        return None
    if 0 < min(image.width, image.height) < MIN_ICON_SIDE:
        return None
    return image


def _sniff(d: bytes) -> IconImage | None:
    if d[:8] == b"\x89PNG\r\n\x1a\n" and len(d) >= 24:
        w, h = struct.unpack(">II", d[16:24])
        return IconImage("image/png", w, h)
    if d[:6] in (b"GIF87a", b"GIF89a") and len(d) >= 10:
        w, h = struct.unpack("<HH", d[6:10])
        return IconImage("image/gif", w, h)
    if d[:4] == b"\x00\x00\x01\x00" and len(d) >= 6:
        count = struct.unpack("<H", d[4:6])[0]
        best = 0
        for i in range(min(count, 32)):
            off = 6 + 16 * i
            if len(d) < off + 16:
                break
            w, h = d[off] or 256, d[off + 1] or 256
            best = max(best, min(w, h))
        return IconImage("image/x-icon", best, best) if best else None
    if d[:3] == b"\xff\xd8\xff":
        return IconImage("image/jpeg", 0, 0)
    if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
        return IconImage("image/webp", 0, 0)
    return None
