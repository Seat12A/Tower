"""Organisation icons stored by Tower and served by gate. See docs/badges.md.

The icon is taken once from the organisation's official website (``fetch_site_icon``,
through the ``WebFetcher`` port: SSRF protection, robots.txt, size cap), stored in
``badge_logos`` by its SHA-256 (``store_logo``) and served by
``GET /v1/badges/logos/{sha256}.{ext}``. The badge's ``logo_url`` points there, so the
extension never loads an image from the organisation's site.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.badge_icons import ICON_TYPES, icon_candidates, sniff_icon
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.security import Actor, Permission
from tower.core.models.badges import BadgeLogo


def logo_path(sha256: str, content_type: str) -> str:
    return f"/v1/badges/logos/{sha256}.{ICON_TYPES[content_type]}"


async def store_logo(session: AsyncSession, actor: Actor, data: bytes, source_url: str) -> str:
    """Store an icon (idempotent: same bytes, same row); returns its gate path."""
    actor.require(Permission.BADGES_MANAGE)
    image = sniff_icon(data)
    if image is None:
        raise InvalidInput("Not an accepted icon", code=ec.BDG_BAD_LOGO, size=len(data))
    sha = hashlib.sha256(data).hexdigest()
    await session.execute(
        insert(BadgeLogo)
        .values(
            sha256=sha,
            content_type=image.content_type,
            data=data,
            width=image.width,
            height=image.height,
            source_url=source_url[:2048],
        )
        .on_conflict_do_nothing(index_elements=["sha256"])
    )
    return logo_path(sha, image.content_type)


async def get_logo(session: AsyncSession, sha256: str, ext: str) -> BadgeLogo:
    """Public read of a stored icon (the extension shows it next to handles)."""
    logo = await session.scalar(select(BadgeLogo).where(BadgeLogo.sha256 == sha256.lower()))
    if logo is None or ICON_TYPES.get(logo.content_type) != ext:
        raise NotFound("Icon not found", code=ec.BDG_LOGO_NOT_FOUND)
    return logo


async def fetch_site_icon(page_fetcher: Any, image_fetcher: Any, site: str) -> tuple[bytes, str]:
    """Best icon of a website: declared icons of the home page, then ``/favicon.ico``.

    ``page_fetcher`` and ``image_fetcher`` are ``WebFetcher`` adapters (HTML, images).
    Raises ``FetchError`` or ``InvalidInput`` (``TWR-BDG-028``) when nothing usable is
    found."""
    from tower.core.ports.web import FetchError

    try:
        page = await page_fetcher.fetch(site)
        candidates = icon_candidates(page.text, page.final_url)
    except FetchError:
        candidates = icon_candidates("", site)
    last: Exception | None = None
    for url in candidates:
        try:
            got = await image_fetcher.fetch(url)
        except FetchError as exc:
            last = exc
            continue
        if sniff_icon(got.body) is not None:
            return got.body, got.final_url
    if last is not None:
        raise last
    raise InvalidInput("No usable icon on the site", code=ec.BDG_BAD_LOGO, site=site[:200])
