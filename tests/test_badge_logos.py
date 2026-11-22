"""Organisation icons: page parsing, image sniffing, storage, public route, seed."""

from __future__ import annotations

import struct
import zlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from tower.core.badge_seed import IconFetch, SeedRow, parse_seed, seed_one
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.badge_icons import icon_candidates, sniff_icon
from tower.core.domain.badges import BadgeCategory
from tower.core.domain.errors import InvalidInput, PermissionDenied
from tower.core.domain.security import Actor
from tower.core.models.badges import Badge, BadgeLogo
from tower.core.models.ingestion import PlatformAccount
from tower.core.ports.web import FetchedPage, FetchError
from tower.core.usecases.badge_logos import fetch_site_icon, store_logo

from .conftest import make_settings


def png(w: int = 64, h: int = 64) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def ico(side: int = 32) -> bytes:
    return b"\x00\x00\x01\x00\x01\x00" + bytes([side, side]) + b"\x00" * 14


def test_icon_candidates_order() -> None:
    html = """<html><head>
      <link rel="icon" href="/small.png" sizes="16x16">
      <link rel="icon" href="/big.png" sizes="192x192">
      <link rel="mask-icon" href="/m.svg">
      <link rel="icon" type="image/svg+xml" href="/v.svg">
      <link rel="apple-touch-icon" href="https://cdn.ex.org/touch.png">
      <link rel="stylesheet" href="/s.css">
    </head></html>"""
    assert icon_candidates(html, "https://ex.org/home") == [
        "https://cdn.ex.org/touch.png",
        "https://ex.org/big.png",
        "https://ex.org/small.png",
        "https://ex.org/favicon.ico",
    ]
    assert icon_candidates("<link rel='icon' href='javascript:x'>", "https://a.b/") == [
        "https://a.b/favicon.ico"
    ]


def test_sniff_icon() -> None:
    image = sniff_icon(png(48, 48))
    assert image is not None and (image.content_type, image.width) == ("image/png", 48)
    image = sniff_icon(ico(0))  # 0 means 256 in an ICO directory
    assert image is not None and image.content_type == "image/x-icon" and image.width == 256
    assert sniff_icon(png(8, 8)) is None  # too small
    assert sniff_icon(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert sniff_icon(b"\xff\xd8\xff" + b"0" * 300_000) is None  # too large
    assert sniff_icon(b"") is None


async def test_store_logo(db: Database) -> None:
    team = Actor.system("test")
    async with db.transaction() as s:
        path = await store_logo(s, team, png(), "https://ex.org/i.png")
        again = await store_logo(s, team, png(), "https://ex.org/other.png")
    assert path == again and path.startswith("/v1/badges/logos/") and path.endswith(".png")
    async with db.session() as s:
        assert len((await s.execute(select(BadgeLogo))).scalars().all()) == 1
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as err:
            await store_logo(s, team, b"<svg/>", "https://ex.org/x.svg")
        assert err.value.code == ec.BDG_BAD_LOGO.code
        with pytest.raises(PermissionDenied):
            await store_logo(s, Actor.anonymous(), png(), "https://ex.org/i.png")


@pytest.fixture
async def api(db: Database) -> AsyncIterator[httpx.AsyncClient]:
    from tower.gate.app import create_app

    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gate") as c:
            yield c


async def test_logo_route(db: Database, api: httpx.AsyncClient) -> None:
    async with db.transaction() as s:
        path = await store_logo(s, Actor.system("test"), png(), "https://ex.org/i.png")
    r = await api.get(path)
    assert r.status_code == 200 and r.content == png()
    assert r.headers["content-type"] == "image/png"
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    r = await api.get(path.replace(".png", ".gif"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-BDG-029"
    r = await api.get("/v1/badges/logos/" + "0" * 64 + ".png")
    assert r.status_code == 404
    r = await api.get("/v1/badges/logos/not-a-hash.png")
    assert r.status_code == 422


class FakeFetcher:
    def __init__(self, pages: dict[str, tuple[str, bytes]]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.calls.append(url)
        if url not in self.pages:
            raise FetchError("HTTP status 404", code=ec.WEB_FETCH_FAILED, url=url)
        ctype, body = self.pages[url]
        text = body.decode() if ctype == "text/html" else ""
        return FetchedPage(url, url, 200, ctype, text, "0", datetime.now(UTC), body)

    async def aclose(self) -> None:
        return None


async def test_fetch_site_icon_falls_back() -> None:
    page = FakeFetcher(
        {"https://ex.org": ("text/html", b"<link rel='apple-touch-icon' href='/t.png'>")}
    )
    images = FakeFetcher(
        {
            "https://ex.org/t.png": ("text/html", b"oops"),
            "https://ex.org/favicon.ico": ("image/x-icon", ico()),
        }
    )
    data, url = await fetch_site_icon(page, images, "https://ex.org")
    assert data == ico() and url == "https://ex.org/favicon.ico"
    with pytest.raises(FetchError):
        await fetch_site_icon(FakeFetcher({}), FakeFetcher({}), "https://none.org")


def test_parse_website() -> None:
    head = "handle,category,name,expertise,review,note,logo,parent,x_id,website\n"
    parsed = parse_seed(head + "a,media,A,,,,,,,https://a.org\nb,media,B,,,,,,,ftp://b\n")
    assert [r.website for r in parsed.rows] == ["https://a.org"] and len(parsed.errors) == 1


async def test_seed_fetches_and_stores_the_icon(db: Database, tmp_path: Path) -> None:
    row = SeedRow(
        2, "Ex", BadgeCategory.MEDIA, ("Ex",), None, False, None, website="https://ex.org"
    )
    icons = IconFetch(
        page=FakeFetcher({"https://ex.org": ("text/html", b"<link rel=icon href=/i.png>")}),
        image=FakeFetcher({"https://ex.org/i.png": ("image/png", png())}),
        base_url="https://gate.example",
    )
    out = await seed_one(
        db, None, row, ttl_s=600, include_review=False, wikidata={"ex": "77"}, icons=icons
    )
    assert out.status == "granted" and "icon" in out.detail
    async with db.session() as s:
        badge = await s.scalar(
            select(Badge).join(PlatformAccount).where(PlatformAccount.external_id == "77")
        )
    assert badge is not None and badge.logo_url is not None
    assert badge.logo_url.startswith("https://gate.example/v1/badges/logos/")
    # A site without a usable icon: badge granted all the same, without logo.
    row2 = SeedRow(
        3, "Ex2", BadgeCategory.MEDIA, ("Ex2",), None, False, None, website="https://none.org"
    )
    icons2 = IconFetch(page=FakeFetcher({}), image=FakeFetcher({}), base_url="https://g.e")
    out = await seed_one(
        db, None, row2, ttl_s=600, include_review=False, wikidata={"ex2": "78"}, icons=icons2
    )
    assert out.status == "granted" and "no icon" in out.detail
