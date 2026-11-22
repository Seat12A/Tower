"""Bulk badge seed from a curated CSV list (``tower badges seed``).

Each row names an X account by handle, the expected display name and the badge
category. The native X id is first looked up on Wikidata (free: P2002 username with its
P6552 id qualifier). Without ``--apply`` nothing is written and X is never called: the
file is validated and the plan (free and paid rows) is printed. With ``--apply`` the
badge is granted through the ``grant_badge`` use case (permission, audit, event).

Safety rules (when in doubt, do not grant):

- an account that already has a badge row is never touched (active, blocked, or
  revoked by the team), except a badge revoked by the system;
- an unambiguous Wikidata id is trusted as is (team decision, 2026-11-22); several
  ids for one handle are ignored and the row goes to X;
- a row not found on Wikidata is resolved with the official X API
  (``resolve_x_handle``: one billable user read on ``provider:x``) and must pass the
  name check (``name_mismatch`` otherwise) and carry an X check (``blue``,
  ``business`` or ``government``; ``no_check`` otherwise);
- such a row flagged ``review`` needs the ``government`` (grey) check.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tower.core.domain.badges import (
    AFFILIABLE_CATEGORIES,
    MAX_TITLE,
    BadgeCategory,
    check_expertise,
    check_text,
    check_url,
    parse_category,
    parse_x_handle,
)
from tower.core.domain.errors import DomainError
from tower.core.domain.platforms import Platform, validate_external_id

if TYPE_CHECKING:
    from tower.core.config import Settings

DEFAULT_SEED = "badge_seed.csv"
SEED_ACTOR = "badges-seed"
COLUMNS = (
    "handle",
    "category",
    "name",
    "expertise",
    "review",
    "note",
    "logo",
    "parent",
    "x_id",
    "website",
)
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class SeedRow:
    line: int
    handle: str
    category: BadgeCategory
    names: tuple[str, ...]
    expertise: str | None
    review: bool
    note: str | None
    logo: str | None = None
    """https URL of the organisation icon, set when the badge is granted."""
    parents: tuple[tuple[str, str | None], ...] = ()
    x_id: str | None = None
    """Native X id frozen in the list (read once from Wikidata): no lookup at all."""
    website: str | None = None
    """Official website: its icon is fetched, stored by Tower and used as ``logo_url``
    when ``logo`` is empty (organisation and media badges)."""
    """(parent handle, title) of the organisations this account is affiliated with."""


@dataclass(slots=True)
class SeedParse:
    rows: list[SeedRow]
    errors: list[str]


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(_fold(text)) if len(w) >= 3}


def name_matches(expected: Sequence[str], display_name: str | None, handle: str | None) -> bool:
    """True when one word (≥ 3 characters, accents and case folded) of an expected name
    appears in the resolved display name.

    Only when the display name has no Latin word at all (Japanese, Arabic, Cyrillic…
    script) does the check fall back to a word of ≥ 4 characters inside the resolved
    handle; such rows should be flagged ``review`` in the list."""
    found = _words(display_name or "")
    folded_handle = _fold(handle or "")
    for name in expected:
        for word in _words(name):
            if word in found or (not found and len(word) >= 4 and word in folded_handle):
                return True
    return False


def parse_seed(text: str) -> SeedParse:
    """Validate a seed CSV (header ``COLUMNS``; ``#`` lines are comments).

    Invalid rows and duplicate handles are reported, never guessed."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if tuple(reader.fieldnames or ()) != COLUMNS:
        return SeedParse([], [f"bad header: expected {','.join(COLUMNS)}"])
    rows: list[SeedRow] = []
    errors: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(reader, start=2):
        label = raw.get("handle") or ""
        try:
            handle = parse_x_handle(label)
            category = parse_category((raw.get("category") or "").strip())
            expertise = check_expertise(category, raw.get("expertise") or None)
        except DomainError as exc:
            errors.append(f"row {index} ({label}): {exc}")
            continue
        try:
            logo = check_url(raw.get("logo") or None, field="logo_url")
            website = _check_website(raw.get("website") or "")
            raw_id = (raw.get("x_id") or "").strip()
            x_id = validate_external_id(Platform.X, raw_id) if raw_id else None
            parents = _parse_parents(raw.get("parent") or "")
        except DomainError as exc:
            errors.append(f"row {index} ({label}): {exc}")
            continue
        names = tuple(n.strip() for n in (raw.get("name") or "").split("|") if n.strip())
        if not names:
            errors.append(f"row {index} ({label}): name required")
            continue
        key = handle.lower()
        if key in seen:
            errors.append(f"row {index} ({label}): duplicate of row {seen[key]}")
            continue
        seen[key] = index
        rows.append(
            SeedRow(
                line=index,
                handle=handle,
                category=category,
                names=names,
                expertise=expertise,
                review=(raw.get("review") or "").strip() == "1",
                note=(raw.get("note") or "").strip() or None,
                logo=logo,
                parents=parents,
                x_id=x_id,
                website=website,
            )
        )
    return SeedParse(*_check_parents(rows, errors))


def _check_website(value: str) -> str | None:
    from urllib.parse import urlsplit

    from tower.core.domain import error_codes as ec
    from tower.core.domain.errors import InvalidInput

    value = value.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(value) > 2048:
        raise InvalidInput("Invalid website", code=ec.GEN_INVALID_INPUT, field="website")
    return value


@dataclass(frozen=True, slots=True)
class IconFetch:
    """Fetchers (``WebFetcher`` port) and public gate origin for organisation icons."""

    page: Any
    image: Any
    base_url: str


def _parse_parents(value: str) -> tuple[tuple[str, str | None], ...]:
    """``Handle=Title|Other`` → ((Handle, Title), (Other, None))."""
    out: list[tuple[str, str | None]] = []
    for part in value.split("|"):
        if not part.strip():
            continue
        handle, _, title = part.partition("=")
        out.append(
            (parse_x_handle(handle), check_text(title or None, field="title", max_chars=MAX_TITLE))
        )
    return tuple(out)


def _check_parents(rows: list[SeedRow], errors: list[str]) -> tuple[list[SeedRow], list[str]]:
    """A parent must be a row of the list with an affiliable category, never the row
    itself. Rows with an invalid parent are reported and dropped."""
    by_handle = {r.handle.lower(): r for r in rows}
    kept: list[SeedRow] = []
    for row in rows:
        bad = None
        for parent, _ in row.parents:
            target = by_handle.get(parent.lower())
            if target is None:
                bad = f"parent @{parent} is not in the list"
            elif target is row:
                bad = "an account cannot be its own parent"
            elif target.category not in AFFILIABLE_CATEGORIES:
                bad = f"parent @{parent} has a non-affiliable category"
        if bad:
            errors.append(f"row {row.line} ({row.handle}): {bad}")
        else:
            kept.append(row)
    return kept, errors


def read_seed(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return resources.files("tower.core.data").joinpath(DEFAULT_SEED).read_text(encoding="utf-8")


@dataclass(slots=True)
class SeedOutcome:
    handle: str
    status: str
    """``granted``, ``exists``, ``name_mismatch``, ``review``, ``no_check``,
    ``not_found``, ``needs_x``."""
    detail: str = ""
    affiliations: list[str] | None = None
    """One line per parent: ``affiliated``, ``affiliation_exists``, ``parent_missing``…"""


async def _existing_badge(db: Any, external_id: str) -> tuple[str, str | None] | None:
    from sqlalchemy import select

    from tower.core.models.badges import Badge
    from tower.core.models.ingestion import PlatformAccount

    async with db.session() as session:
        row = (
            await session.execute(
                select(Badge.status, Badge.ended_by)
                .join(PlatformAccount, PlatformAccount.id == Badge.account_id)
                .where(PlatformAccount.platform == "x", PlatformAccount.external_id == external_id)
            )
        ).first()
    return (row[0], row[1]) if row else None


async def _known_active(db: Any, handle: str) -> str | None:
    """Native id of an account with an active badge whose stored handle matches (saves an
    X read; also finds the parent organisation of an affiliation)."""
    from sqlalchemy import func, select

    from tower.core.models.badges import Badge
    from tower.core.models.ingestion import PlatformAccount

    async with db.session() as session:
        found = await session.scalar(
            select(PlatformAccount.external_id)
            .join(Badge, PlatformAccount.id == Badge.account_id)
            .where(
                PlatformAccount.platform == "x",
                func.lower(PlatformAccount.handle) == handle.lower(),
                Badge.status == "active",
            )
            .limit(1)
        )
    return str(found) if found is not None else None


def _blocks_grant(existing: tuple[str, str | None] | None) -> str | None:
    if existing is None:
        return None
    status, ended_by = existing
    if status == "revoked" and (ended_by or "").startswith("system:"):
        return None
    return f"badge {status}" + (f" by {ended_by}" if ended_by and status != "active" else "")


CHECKED_TYPES = frozenset({"blue", "business", "government"})


async def lookup_wikidata(source: Any, handles: Sequence[str]) -> dict[str, str | None]:
    """Native X id of each handle from Wikidata (P2002 + P6552), keyed by lowercase
    handle. ``None``: several different ids (ambiguous, never used). Handles are asked
    as given and in lowercase (WDQS compares strings exactly)."""
    from tower.core.domain.errors import DomainError
    from tower.core.domain.platforms import Platform, validate_external_id

    ids: dict[str, set[str]] = {}
    unique = list(dict.fromkeys(handles))
    for start in range(0, len(unique), 100):
        chunk = unique[start : start + 100]
        asked = list(dict.fromkeys([*chunk, *(h.lower() for h in chunk)]))
        for account in await source.accounts_by_handles(asked):
            if account.handle is None:
                continue
            try:
                xid = validate_external_id(Platform.X, account.x_id)
            except DomainError:
                continue
            ids.setdefault(account.handle.lower(), set()).add(xid)
    return {h: (next(iter(v)) if len(v) == 1 else None) for h, v in ids.items()}


async def _grant(
    db: Any,
    row: SeedRow,
    external_id: str,
    handle: str,
    extra_note: str,
    icons: IconFetch | None = None,
) -> tuple[str | None, str]:
    """Grant through the use case. Returns (error text on a conflict, icon note).

    An organisation or media row without ``logo`` but with a ``website`` gets the site's
    icon, fetched now (outside any transaction) and stored in the grant's transaction."""
    from tower.core.domain.badges import AccountRef
    from tower.core.domain.errors import Conflict
    from tower.core.domain.platforms import Platform
    from tower.core.domain.security import Actor
    from tower.core.ports.web import FetchError
    from tower.core.usecases import badge_logos
    from tower.core.usecases import badges as uc

    actor = Actor.system(SEED_ACTOR)
    affiliable = row.category in AFFILIABLE_CATEGORIES
    icon: tuple[bytes, str] | None = None
    icon_note = ""
    if affiliable and row.logo is None and row.website and icons is not None:
        try:
            icon = await badge_logos.fetch_site_icon(icons.page, icons.image, row.website)
        except (FetchError, DomainError) as exc:
            icon_note = f"; no icon ({str(exc)[:80]})"
    note = f"seed {SEED_ACTOR} ({extra_note})" + (f": {row.note}" if row.note else "")
    try:
        async with db.transaction() as session:
            logo = row.logo if affiliable else None
            if icon is not None and icons is not None:
                path = await badge_logos.store_logo(session, actor, icon[0], icon[1])
                logo = icons.base_url + path
                icon_note = f"; icon {path.rsplit('/', 1)[-1][:12]}…"
            await uc.grant_badge(
                session,
                actor,
                AccountRef(Platform.X, external_id),
                category=row.category.value,
                expertise=row.expertise,
                note=note,
                handle=handle,
                logo_url=logo,
            )
    except Conflict as exc:
        return str(exc), ""
    return None, icon_note


async def _affiliate(
    db: Any,
    member_xid: str,
    member_handle: str,
    parent: str,
    title: str | None,
    wikidata: dict[str, str | None] | None,
) -> str:
    """Affiliate a member with an active badge to a parent organisation of the list.

    Validated at once by the team (seed actor). Never created when any affiliation row
    already exists for the pair: an affiliation ended by the team or the organisation is
    not reopened."""
    from sqlalchemy import select

    from tower.core.domain.badges import AccountRef
    from tower.core.domain.platforms import Platform
    from tower.core.domain.security import Actor
    from tower.core.models.badges import Badge, BadgeAffiliation
    from tower.core.models.ingestion import PlatformAccount
    from tower.core.usecases.badge_affiliations import create_affiliation

    parent_xid = await _known_active(db, parent) or (wikidata or {}).get(parent.lower())
    if parent_xid is None:
        return f"parent_missing @{parent} (no active badge yet: rerun after it)"
    async with db.session() as session:
        org_badge = await session.scalar(
            select(Badge.id)
            .join(PlatformAccount, PlatformAccount.id == Badge.account_id)
            .where(
                PlatformAccount.platform == "x",
                PlatformAccount.external_id == parent_xid,
                Badge.status == "active",
            )
        )
        if org_badge is None:
            return f"parent_missing @{parent} (no active badge yet: rerun after it)"
        row = await session.scalar(
            select(BadgeAffiliation.status)
            .join(PlatformAccount, PlatformAccount.id == BadgeAffiliation.member_account_id)
            .where(
                PlatformAccount.platform == "x",
                PlatformAccount.external_id == member_xid,
                BadgeAffiliation.org_badge_id == org_badge,
            )
        )
    if row is not None:
        return f"affiliation_exists @{parent} ({row})"
    try:
        async with db.transaction() as session:
            await create_affiliation(
                session,
                Actor.system(SEED_ACTOR),
                AccountRef(Platform.X, member_xid),
                org_ref=AccountRef(Platform.X, parent_xid),
                title=title,
                validate=True,
                handle=member_handle,
            )
    except DomainError as exc:
        return f"affiliation_refused @{parent}: {exc}"
    return f"affiliated @{parent}" + (f" ({title})" if title else "")


async def seed_one(
    db: Any,
    client: Any,
    row: SeedRow,
    *,
    ttl_s: int,
    include_review: bool,
    wikidata: dict[str, str | None] | None = None,
    icons: IconFetch | None = None,
) -> SeedOutcome:
    """Resolve and grant one row, then affiliate it to its parents (see the module
    docstring for the safety rules). Affiliations need the member's badge to be active."""
    outcome, member_xid = await _seed_badge(
        db,
        client,
        row,
        ttl_s=ttl_s,
        include_review=include_review,
        wikidata=wikidata,
        icons=icons,
    )
    if member_xid is not None and row.parents:
        outcome.affiliations = [
            await _affiliate(db, member_xid, row.handle, parent, title, wikidata)
            for parent, title in row.parents
        ]
    return outcome


async def _seed_badge(
    db: Any,
    client: Any,
    row: SeedRow,
    *,
    ttl_s: int,
    include_review: bool,
    wikidata: dict[str, str | None] | None,
    icons: IconFetch | None = None,
) -> tuple[SeedOutcome, str | None]:
    """Outcome and, when the badge is active afterwards, the member's native id.

    An unambiguous Wikidata id is used as is, without any X call. Otherwise the handle
    is resolved with the X API and the account must carry an X check."""
    from tower.core.domain.errors import NotFound
    from tower.core.domain.security import Actor
    from tower.core.usecases.badge_sources import resolve_x_handle

    known = await _known_active(db, row.handle)
    if known is not None:
        return SeedOutcome(row.handle, "exists", "badge active (stored handle)"), known
    wd_id = row.x_id or (wikidata or {}).get(row.handle.lower())
    via = "list" if row.x_id else "wikidata"
    if wd_id is not None:
        existing = await _existing_badge(db, wd_id)
        active = wd_id if existing is not None and existing[0] == "active" else None
        blocked = _blocks_grant(existing)
        if blocked:
            return SeedOutcome(row.handle, "exists", f"{blocked}; id={wd_id} ({via})"), active
        error, icon_note = await _grant(db, row, wd_id, row.handle, via, icons)
        if error:
            return SeedOutcome(row.handle, "exists", f"{error}; id={wd_id} ({via})"), None
        detail = f"{row.category.value}; id={wd_id} ({via}){icon_note}"
        return SeedOutcome(row.handle, "granted", detail), wd_id
    if client is None:
        return SeedOutcome(row.handle, "needs_x", "not on Wikidata; X API not configured"), None
    try:
        found = await resolve_x_handle(
            db, Actor.system(SEED_ACTOR), client, row.handle, ttl_s=ttl_s
        )
    except NotFound:
        return SeedOutcome(row.handle, "not_found"), None
    xid = found["external_id"]
    vtype = found["verified_type"]
    shown = f"{found['display_name']!r} id={xid} x={vtype}"
    existing = await _existing_badge(db, xid)
    blocked = _blocks_grant(existing)
    if blocked:
        active = xid if existing is not None and existing[0] == "active" else None
        return SeedOutcome(row.handle, "exists", f"{blocked}; {shown}"), active
    if not name_matches(row.names, found["display_name"], found["handle"]):
        detail = f"expected {row.names[0]!r}; {shown}"
        return SeedOutcome(row.handle, "name_mismatch", detail), None
    if row.review and not include_review and vtype != "government":
        return SeedOutcome(row.handle, "review", shown), None
    if vtype not in CHECKED_TYPES:
        return SeedOutcome(row.handle, "no_check", shown), None
    error, icon_note = await _grant(db, row, xid, found["handle"], f"x {vtype}", icons)
    if error:
        return SeedOutcome(row.handle, "exists", f"{error}; {shown}"), None
    return SeedOutcome(row.handle, "granted", f"{row.category.value}; {shown}{icon_note}"), xid


def _icon_fetch(settings: Settings, base_url: str) -> IconFetch:
    from tower.core.adapters.http_fetcher import HttpFetcher
    from tower.core.domain.badge_icons import FETCH_TYPES, MAX_ICON_BYTES

    return IconFetch(
        page=HttpFetcher(user_agent=settings.http_user_agent, timeout=10.0),
        image=HttpFetcher(
            user_agent=settings.http_user_agent,
            timeout=10.0,
            max_bytes=MAX_ICON_BYTES,
            binary_types=FETCH_TYPES,
        ),
        base_url=base_url,
    )


def _x_client(settings: Settings) -> Any:
    if settings.x_bearer_token is None:
        return None
    from tower.core.adapters.x_client import XPlatformClient

    return XPlatformClient(
        settings.x_bearer_token.get_secret_value(),
        api_base=settings.x_api_base,
        post_read_usd_micros=round(settings.x_price_post_read_usd * 1e6),
        user_read_usd_micros=round(settings.x_price_user_read_usd * 1e6),
    )


def print_plan(
    parsed: SeedParse,
    rows: Sequence[SeedRow],
    wikidata: dict[str, str | None] | None,
    user_read_usd: float,
) -> None:
    def route(row: SeedRow) -> str:
        if row.x_id:
            return "list"
        if wikidata is None:
            return "?"
        key = row.handle.lower()
        if key in wikidata:
            return "wikidata" if wikidata[key] is not None else "x(ambig)"
        return "x"

    for row in rows:
        flag = " [review]" if row.review else ""
        extra = f" ({row.expertise})" if row.expertise else ""
        print(
            f"plan  @{row.handle:<16} {row.category.value:<13} {route(row):<9}"
            f" {row.names[0]}{extra}{flag}"
        )
    for error in parsed.errors:
        print(f"invalid  {error}")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.category.value] = counts.get(row.category.value, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    review = sum(1 for r in rows if r.review)
    print(f"Plan: {len(rows)} rows ({summary}); review={review}; invalid={len(parsed.errors)}.")
    if wikidata is not None:
        paid = sum(1 for r in rows if route(r) not in ("wikidata", "list"))
        frozen = sum(1 for r in rows if r.x_id)
        print(
            f"Ids frozen in the list: {frozen}; from Wikidata: {len(rows) - paid - frozen}"
            f" (free); X reads at most: {paid}"
            f" (≈ {paid * user_read_usd:.2f} USD at TOWER_X_PRICE_USER_READ_USD)."
        )


async def run_seed(
    settings: Settings,
    *,
    path: str | None,
    apply: bool,
    category: str | None,
    limit: int | None,
    include_review: bool,
    use_wikidata: bool = True,
    wikidata_source: Any = None,
    icons: IconFetch | None = None,
) -> int:
    """CLI helper. Returns the process exit code (1 on invalid rows, Wikidata or X errors)."""
    from tower.core.ports.knowledge import SourceUnavailable

    parsed = parse_seed(read_seed(path))
    rows = [r for r in parsed.rows if category is None or r.category.value == category]
    if limit is not None:
        rows = rows[:limit]
    wikidata: dict[str, str | None] | None = None
    if use_wikidata:
        source = wikidata_source
        if source is None:
            from tower.core.adapters.wikidata_badges import WikidataSparqlSource

            source = WikidataSparqlSource(endpoint=settings.badges_wikidata_endpoint)
        try:
            wikidata = await lookup_wikidata(source, [r.handle for r in rows if not r.x_id])
        except SourceUnavailable as exc:
            # Never fall back silently to paid X reads.
            print(f"Wikidata unavailable ({exc}): retry later, or pass --no-wikidata.")
            return 1
        finally:
            if wikidata_source is None:
                await source.aclose()
    # Frozen ids of the whole list (parents may be outside a --category filter).
    frozen_ids = {r.handle.lower(): r.x_id for r in parsed.rows if r.x_id}
    if frozen_ids:
        wikidata = {**(wikidata or {}), **frozen_ids}
    if not apply:
        print_plan(parsed, rows, wikidata, settings.x_price_user_read_usd)
        print("Dry run: no X call, nothing written. Add --apply to grant.")
        return 1 if parsed.errors else 0
    client = _x_client(settings)
    if client is None:
        print("TOWER_X_BEARER_TOKEN is not set: rows not on Wikidata are skipped (needs_x).")
    own_icons = icons is None and settings.public_gate_url is not None
    if own_icons and settings.public_gate_url is not None:
        icons = _icon_fetch(settings, settings.public_gate_url)
    elif icons is None:
        print("TOWER_PUBLIC_GATE_URL is not set: no organisation icon is fetched.")
    from tower.core.db.database import Database
    from tower.core.domain.errors import Conflict

    # Organisations first, so that their members can be affiliated in the same run.
    rows = sorted(rows, key=lambda r: bool(r.parents))
    db = Database(settings)
    counts: dict[str, int] = {}
    code = 0
    try:
        for row in rows:
            try:
                outcome = await seed_one(
                    db,
                    client,
                    row,
                    ttl_s=settings.budget_reservation_ttl_s,
                    include_review=include_review,
                    wikidata=wikidata,
                    icons=icons,
                )
            except Conflict as exc:
                # X unavailable or budget exhausted: stop, a rerun resumes safely.
                print(f"stop  @{row.handle}: {exc}")
                code = 1
                break
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            print(f"{outcome.status:<13} @{outcome.handle:<16} {outcome.detail}")
            for line in outcome.affiliations or ():
                key = line.split(" ", 1)[0]
                counts[key] = counts.get(key, 0) + 1
                print(f"  {line}")
            if "(wikidata)" not in outcome.detail and "(list)" not in outcome.detail:
                await asyncio.sleep(0.1)
    finally:
        if client is not None:
            await client.aclose()
        if own_icons and icons is not None:
            await icons.page.aclose()
            await icons.image.aclose()
        await db.dispose()
    print("Done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return code
