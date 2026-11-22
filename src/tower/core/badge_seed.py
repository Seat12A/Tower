"""Bulk badge seed from a curated CSV list (``tower badges seed``).

Each row names an X account by handle, the expected display name and the badge
category. Without ``--apply`` nothing is called nor written: the file is validated and
the plan is printed. With ``--apply`` every handle is resolved once with the official X
API (``resolve_x_handle``: one billable user read on ``provider:x``), then the badge is
granted through the ``grant_badge`` use case (permission, audit, event).

Safety rules (when in doubt, do not grant):

- an account that already has a badge row is never touched (active, blocked, or
  revoked by the team), except a badge revoked by the system;
- the resolved display name or handle must share a word with the expected name,
  otherwise the row is skipped (``name_mismatch``) for a manual check;
- a row flagged ``review`` is only granted when X itself marks the account
  ``government`` (grey check); otherwise it is listed for a manual check.
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
    BadgeCategory,
    check_expertise,
    parse_category,
    parse_x_handle,
)
from tower.core.domain.errors import DomainError

if TYPE_CHECKING:
    from tower.core.config import Settings

DEFAULT_SEED = "badge_seed.csv"
SEED_ACTOR = "badges-seed"
COLUMNS = ("handle", "category", "name", "expertise", "review", "note")
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
    appears in the resolved display name, or inside the resolved handle."""
    found = _words(display_name or "")
    folded_handle = _fold(handle or "")
    for name in expected:
        for word in _words(name):
            if word in found or (len(word) >= 4 and word in folded_handle):
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
            )
        )
    return SeedParse(rows, errors)


def read_seed(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return resources.files("tower.core.data").joinpath(DEFAULT_SEED).read_text(encoding="utf-8")


@dataclass(slots=True)
class SeedOutcome:
    handle: str
    status: str
    """``granted``, ``exists``, ``name_mismatch``, ``review``, ``not_found``, ``error``."""
    detail: str = ""


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


async def _known_active(db: Any, handle: str) -> bool:
    """Active badge on an account whose stored handle matches (saves an X read)."""
    from sqlalchemy import func, select

    from tower.core.models.badges import Badge
    from tower.core.models.ingestion import PlatformAccount

    async with db.session() as session:
        found = await session.scalar(
            select(Badge.id)
            .join(PlatformAccount, PlatformAccount.id == Badge.account_id)
            .where(
                PlatformAccount.platform == "x",
                func.lower(PlatformAccount.handle) == handle.lower(),
                Badge.status == "active",
            )
            .limit(1)
        )
    return found is not None


def _blocks_grant(existing: tuple[str, str | None] | None) -> str | None:
    if existing is None:
        return None
    status, ended_by = existing
    if status == "revoked" and (ended_by or "").startswith("system:"):
        return None
    return f"badge {status}" + (f" by {ended_by}" if ended_by and status != "active" else "")


async def seed_one(
    db: Any, client: Any, row: SeedRow, *, ttl_s: int, include_review: bool
) -> SeedOutcome:
    """Resolve and grant one row (see module docstring for the safety rules)."""
    from tower.core.domain.badges import AccountRef
    from tower.core.domain.errors import Conflict, NotFound
    from tower.core.domain.platforms import Platform
    from tower.core.domain.security import Actor
    from tower.core.usecases import badges as uc
    from tower.core.usecases.badge_sources import resolve_x_handle

    actor = Actor.system(SEED_ACTOR)
    if await _known_active(db, row.handle):
        return SeedOutcome(row.handle, "exists", "badge active (stored handle)")
    try:
        found = await resolve_x_handle(db, actor, client, row.handle, ttl_s=ttl_s)
    except NotFound:
        return SeedOutcome(row.handle, "not_found")
    shown = f"{found['display_name']!r} id={found['external_id']} x={found['verified_type']}"
    blocked = _blocks_grant(await _existing_badge(db, found["external_id"]))
    if blocked:
        return SeedOutcome(row.handle, "exists", f"{blocked}; {shown}")
    if not name_matches(row.names, found["display_name"], found["handle"]):
        return SeedOutcome(row.handle, "name_mismatch", f"expected {row.names[0]!r}; {shown}")
    if row.review and not include_review and found["verified_type"] != "government":
        return SeedOutcome(row.handle, "review", shown)
    note = f"seed {SEED_ACTOR}" + (f": {row.note}" if row.note else "")
    try:
        async with db.transaction() as session:
            await uc.grant_badge(
                session,
                actor,
                AccountRef(Platform.X, found["external_id"]),
                category=row.category.value,
                expertise=row.expertise,
                note=note,
                handle=found["handle"],
            )
    except Conflict as exc:
        return SeedOutcome(row.handle, "exists", f"{exc}; {shown}")
    return SeedOutcome(row.handle, "granted", f"{row.category.value}; {shown}")


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


def print_plan(parsed: SeedParse, rows: Sequence[SeedRow]) -> None:
    for row in rows:
        flag = " [review]" if row.review else ""
        extra = f" ({row.expertise})" if row.expertise else ""
        print(f"plan  @{row.handle:<16} {row.category.value:<13} {row.names[0]}{extra}{flag}")
    for error in parsed.errors:
        print(f"invalid  {error}")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.category.value] = counts.get(row.category.value, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    review = sum(1 for r in rows if r.review)
    print(f"Plan: {len(rows)} rows ({summary}); review={review}; invalid={len(parsed.errors)}.")


async def run_seed(
    settings: Settings,
    *,
    path: str | None,
    apply: bool,
    category: str | None,
    limit: int | None,
    include_review: bool,
) -> int:
    """CLI helper. Returns the process exit code (1 on invalid rows or X errors)."""
    parsed = parse_seed(read_seed(path))
    rows = [r for r in parsed.rows if category is None or r.category.value == category]
    if limit is not None:
        rows = rows[:limit]
    if not apply:
        print_plan(parsed, rows)
        print("Dry run: nothing called, nothing written. Add --apply to resolve and grant.")
        return 1 if parsed.errors else 0
    client = _x_client(settings)
    if client is None:
        print("TOWER_X_BEARER_TOKEN is not set: run this on the gate service.")
        return 1
    from tower.core.db.database import Database
    from tower.core.domain.errors import Conflict

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
                )
            except Conflict as exc:
                # X unavailable or budget exhausted: stop, a rerun resumes safely.
                print(f"stop  @{row.handle}: {exc}")
                code = 1
                break
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            print(f"{outcome.status:<13} @{outcome.handle:<16} {outcome.detail}")
            await asyncio.sleep(0.1)
    finally:
        await client.aclose()
        await db.dispose()
    print("Done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return code
