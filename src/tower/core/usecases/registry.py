"""Editable, versioned reliability registry and country registries (lot 6).

A source tier rests on the editorial process, never on political orientation: every tier
change, deactivation and update needs a non-empty ``reason`` and ``statutory_independence``
needs an external ``independence_basis``. Each write of ``source_registry`` bumps
``version``, stores a full snapshot in ``source_registry_versions`` (the audit trail and the
published history) and publishes ``source_registry.changed`` in the same transaction.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import orjson
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Conflict, InvalidInput, NotFound
from tower.core.domain.factcheck.atlas import (
    ControlOrigin,
    ElectionKind,
    IndependenceBasis,
    JurisdictionSourceType,
    LegalAccess,
    RegistryChange,
    valid_country,
)
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.jobs.queue import enqueue
from tower.core.models.atlas import (
    ElectionEntry,
    JurisdictionSource,
    LegalPortal,
    SourceRegistryVersion,
)
from tower.core.models.factcheck import REGISTRY_KINDS, TIERS, SourceRegistryEntry

__all__ = [
    "apply_control_seed",
    "create_entry",
    "deactivate_entry",
    "entry_history",
    "entry_snapshot",
    "export_registry",
    "get_entry",
    "list_elections",
    "list_jurisdiction_sources",
    "list_legal_portals",
    "list_registry",
    "normalize_domain",
    "request_control_seed",
    "update_entry",
    "upsert_election",
    "upsert_jurisdiction_source",
    "upsert_legal_portal",
]

_QID = re.compile(r"^Q[1-9][0-9]{0,15}$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_MAX_TEXT = 500
_MAX_ALIASES = 50
_UPDATABLE = frozenset(
    {
        "publisher",
        "tier",
        "kind",
        "country",
        "aliases",
        "qid",
        "controlled_by",
        "controlled_by_qid",
        "controlled_by_aliases",
        "statutory_independence",
        "independence_basis",
    }
)


def _invalid(message: str, **details: object) -> InvalidInput:
    return InvalidInput(message, code=ec.ATL_REGISTRY_INVALID, **details)


def normalize_domain(raw: str) -> str:
    """``https://www.Example.org:8080/path`` -> ``example.org``; must contain a dot."""
    value = raw.strip().lower()
    value = _SCHEME.sub("", value)
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    value = value.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if value.startswith("www."):
        value = value[4:]
    if not _DOMAIN.fullmatch(value):
        raise _invalid(
            "Invalid domain (a registrable domain or host suffix is expected)", field="domain"
        )
    return value


def _reason(value: str | None, *, field: str = "reason") -> str:
    text = (value or "").strip()
    if not text:
        raise _invalid("A reason is required", field=field)
    if len(text) > 2000:
        raise _invalid("Reason is too long", field=field)
    return text


def _text(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _invalid(f"{field} is required", field=field)
        return None
    if not isinstance(value, str) or len(value.strip()) > _MAX_TEXT:
        raise _invalid(f"Invalid {field}", field=field)
    return value.strip()


def _tier(value: object) -> str:
    if value not in TIERS:
        raise _invalid("Unknown tier", field="tier", allowed=list(TIERS))
    return str(value)


def _kind(value: object) -> str:
    if value not in REGISTRY_KINDS:
        raise _invalid("Unknown kind", field="kind", allowed=list(REGISTRY_KINDS))
    return str(value)


def _country(value: object, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise _invalid("country is required", field="country")
        return None
    if not isinstance(value, str) or not valid_country(value):
        raise _invalid("Invalid country code", field="country")
    return value


def _qid(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _QID.fullmatch(value):
        raise _invalid("Invalid Wikidata QID", field=field)
    return value


def _aliases(value: object, field: str = "aliases") -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _invalid("A list of names is expected", field=field)
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _invalid("A list of names is expected", field=field)
        name = item.strip()
        if name and len(name) <= _MAX_TEXT and name not in out:
            out.append(name)
    if len(out) > _MAX_ALIASES:
        raise _invalid("Too many aliases", field=field)
    return out


def _basis(value: object) -> str | None:
    if value is None or value == "":
        return None
    allowed = [b.value for b in IndependenceBasis]
    if value not in allowed:
        raise _invalid("Unknown independence basis", field="independence_basis", allowed=allowed)
    return str(value)


def entry_snapshot(entry: SourceRegistryEntry) -> dict[str, Any]:
    """Full row as JSON-compatible data (history snapshot and export)."""
    return {
        "id": str(entry.id),
        "domain": entry.domain,
        "publisher": entry.publisher,
        "tier": entry.tier,
        "kind": entry.kind,
        "country": entry.country,
        "reason": entry.reason,
        "origin": entry.origin,
        "version": entry.version,
        "aliases": list(entry.aliases or []),
        "qid": entry.qid,
        "controlled_by": entry.controlled_by,
        "controlled_by_qid": entry.controlled_by_qid,
        "controlled_by_aliases": list(entry.controlled_by_aliases or []),
        "control_origin": entry.control_origin,
        "statutory_independence": entry.statutory_independence,
        "independence_basis": entry.independence_basis,
        "active": entry.active,
        "updated_by": entry.updated_by,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "reviewed_at": entry.reviewed_at.isoformat() if entry.reviewed_at else None,
    }


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _record(
    session: AsyncSession,
    entry: SourceRegistryEntry,
    change: RegistryChange,
    reason: str | None,
    by: str,
) -> None:
    """Snapshot + event, in the caller's transaction."""
    await session.flush()
    await session.refresh(entry)
    session.add(
        SourceRegistryVersion(
            registry_id=entry.id,
            version=entry.version,
            snapshot=entry_snapshot(entry),
            change=change.value,
            reason=reason,
            changed_by=by,
        )
    )
    await publish(
        session,
        names.SOURCE_REGISTRY_CHANGED,
        {
            "registry_id": str(entry.id),
            "domain": entry.domain,
            "change": change.value,
            "version": entry.version,
            "tier": entry.tier,
            "controlled_by": entry.controlled_by,
            "changed_by": by,
        },
        aggregate_type="source_registry",
        aggregate_id=entry.id,
        actor=by,
    )


# --- reads -----------------------------------------------------------------------------


async def list_registry(
    session: AsyncSession,
    actor: Actor,
    *,
    country: str | None = None,
    tier: str | None = None,
    active: bool | None = True,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[SourceRegistryEntry]:
    actor.require(Permission.REGISTRY_READ)
    stmt = select(SourceRegistryEntry).order_by(SourceRegistryEntry.domain)
    if country:
        stmt = stmt.where(SourceRegistryEntry.country == country.upper())
    if tier:
        stmt = stmt.where(SourceRegistryEntry.tier == tier)
    if active is not None:
        stmt = stmt.where(SourceRegistryEntry.active == active)
    if q and q.strip():
        pattern = (
            "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        stmt = stmt.where(
            or_(
                SourceRegistryEntry.domain.ilike(pattern, escape="\\"),
                SourceRegistryEntry.publisher.ilike(pattern, escape="\\"),
            )
        )
    rows = await session.scalars(stmt.limit(max(1, min(limit, 500))).offset(max(0, offset)))
    return list(rows)


async def _load(
    session: AsyncSession, ref: str | int, *, lock: bool = False
) -> SourceRegistryEntry:
    text = str(ref).strip()
    stmt = select(SourceRegistryEntry)
    if text.isdigit():
        stmt = stmt.where(SourceRegistryEntry.id == int(text))
    else:
        try:
            domain = normalize_domain(text)
        except InvalidInput:
            raise NotFound("Registry entry not found", code=ec.ATL_REGISTRY_NOT_FOUND) from None
        stmt = stmt.where(SourceRegistryEntry.domain == domain)
    if lock:
        stmt = stmt.with_for_update()
    entry = await session.scalar(stmt)
    if entry is None:
        raise NotFound("Registry entry not found", code=ec.ATL_REGISTRY_NOT_FOUND)
    return entry


async def get_entry(session: AsyncSession, actor: Actor, ref: str | int) -> SourceRegistryEntry:
    """By numeric id or by domain."""
    actor.require(Permission.REGISTRY_READ)
    return await _load(session, ref)


async def entry_history(
    session: AsyncSession, actor: Actor, ref: str | int
) -> list[SourceRegistryVersion]:
    actor.require(Permission.REGISTRY_READ)
    entry = await _load(session, ref)
    rows = await session.scalars(
        select(SourceRegistryVersion)
        .where(SourceRegistryVersion.registry_id == entry.id)
        .order_by(SourceRegistryVersion.version.desc())
    )
    return list(rows)


async def export_registry(session: AsyncSession, actor: Actor) -> dict[str, Any]:
    """The published registry: every active entry, with a content digest."""
    actor.require(Permission.REGISTRY_READ)
    rows = await session.scalars(
        select(SourceRegistryEntry)
        .where(SourceRegistryEntry.active.is_(True))
        .order_by(SourceRegistryEntry.domain)
    )
    keys = (
        "id",
        "domain",
        "publisher",
        "tier",
        "kind",
        "country",
        "reason",
        "controlled_by",
        "control_origin",
        "statutory_independence",
        "independence_basis",
        "version",
    )
    entries = [{k: entry_snapshot(e)[k] for k in keys} for e in rows]
    digest = hashlib.sha256(orjson.dumps(entries, option=orjson.OPT_SORT_KEYS)).hexdigest()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(entries),
        "digest": digest,
        "entries": entries,
    }


# --- writes ----------------------------------------------------------------------------


async def create_entry(
    session: AsyncSession,
    actor: Actor,
    *,
    domain: str,
    publisher: str,
    tier: str,
    kind: str,
    country: str | None,
    reason: str,
    aliases: Sequence[str] = (),
    qid: str | None = None,
    controlled_by: str | None = None,
    controlled_by_qid: str | None = None,
    controlled_by_aliases: Sequence[str] = (),
    statutory_independence: bool = False,
    independence_basis: str | None = None,
) -> SourceRegistryEntry:
    actor.require(Permission.REGISTRY_WRITE)
    norm = normalize_domain(domain)
    why = _reason(reason)
    basis = _basis(independence_basis)
    if statutory_independence and basis is None:
        raise _invalid(
            "statutory_independence requires an independence_basis", field="independence_basis"
        )
    if await session.scalar(
        select(SourceRegistryEntry.id).where(SourceRegistryEntry.domain == norm)
    ):
        raise Conflict("This domain is already in the registry", code=ec.ATL_REGISTRY_CONFLICT)
    control = _text(controlled_by, "controlled_by", required=False)
    entry = SourceRegistryEntry(
        domain=norm,
        publisher=str(_text(publisher, "publisher")),
        tier=_tier(tier),
        kind=_kind(kind),
        country=_country(country),
        reason=why,
        origin="team",
        version=1,
        aliases=_aliases(aliases),
        qid=_qid(qid, "qid"),
        controlled_by=control,
        controlled_by_qid=_qid(controlled_by_qid, "controlled_by_qid") if control else None,
        controlled_by_aliases=_aliases(controlled_by_aliases, "controlled_by_aliases")
        if control
        else [],
        control_origin=ControlOrigin.TEAM.value if control else None,
        statutory_independence=bool(statutory_independence),
        independence_basis=basis,
        active=True,
        updated_by=actor.label,
        updated_at=_now(),
    )
    session.add(entry)
    await _record(session, entry, RegistryChange.CREATED, why, actor.label)
    return entry


def _apply_changes(entry: SourceRegistryEntry, changes: Mapping[str, Any], why: str) -> bool:
    """Validate and apply ``changes``; return whether anything changed."""
    unknown = sorted(set(changes) - _UPDATABLE)
    if unknown:
        raise _invalid("Field not editable", fields=unknown)
    values: dict[str, Any] = {}
    if "publisher" in changes:
        values["publisher"] = _text(changes["publisher"], "publisher")
    if "tier" in changes:
        values["tier"] = _tier(changes["tier"])
        if values["tier"] != entry.tier:
            values["reason"] = why  # current justification of the tier
    if "kind" in changes:
        values["kind"] = _kind(changes["kind"])
    if "country" in changes:
        values["country"] = _country(changes["country"])
    if "aliases" in changes:
        values["aliases"] = _aliases(changes["aliases"])
    if "qid" in changes:
        values["qid"] = _qid(changes["qid"], "qid")
    if "controlled_by" in changes:
        control = _text(changes["controlled_by"], "controlled_by", required=False)
        values["controlled_by"] = control
        values["control_origin"] = ControlOrigin.TEAM.value  # also when cleared: no re-seed
        if "controlled_by_qid" not in changes:
            values["controlled_by_qid"] = None
        if "controlled_by_aliases" not in changes:
            values["controlled_by_aliases"] = []
    if "controlled_by_qid" in changes:
        values["controlled_by_qid"] = _qid(changes["controlled_by_qid"], "controlled_by_qid")
        values.setdefault("control_origin", ControlOrigin.TEAM.value)
    if "controlled_by_aliases" in changes:
        values["controlled_by_aliases"] = _aliases(
            changes["controlled_by_aliases"], "controlled_by_aliases"
        )
        values.setdefault("control_origin", ControlOrigin.TEAM.value)
    if "independence_basis" in changes:
        values["independence_basis"] = _basis(changes["independence_basis"])
    if "statutory_independence" in changes:
        values["statutory_independence"] = bool(changes["statutory_independence"])
    flag = values.get("statutory_independence", entry.statutory_independence)
    basis = values.get("independence_basis", entry.independence_basis)
    if flag and basis is None:
        raise _invalid(
            "statutory_independence requires an independence_basis", field="independence_basis"
        )
    changed = {k: v for k, v in values.items() if getattr(entry, k) != v}
    for key, value in changed.items():
        setattr(entry, key, value)
    return bool(changed)


async def update_entry(
    session: AsyncSession,
    actor: Actor,
    entry_id: str | int,
    *,
    expected_version: int,
    changes: Mapping[str, Any],
    reason: str,
) -> SourceRegistryEntry:
    actor.require(Permission.REGISTRY_WRITE)
    why = _reason(reason)
    entry = await _load(session, entry_id, lock=True)
    if entry.version != expected_version:
        raise Conflict(
            "Registry entry changed meanwhile",
            code=ec.ATL_REGISTRY_CONFLICT,
            current_version=entry.version,
        )
    if not _apply_changes(entry, changes, why):
        return entry
    entry.version += 1
    entry.updated_by = actor.label
    entry.updated_at = _now()
    await _record(session, entry, RegistryChange.UPDATED, why, actor.label)
    return entry


async def deactivate_entry(
    session: AsyncSession,
    actor: Actor,
    entry_id: str | int,
    *,
    expected_version: int,
    reason: str,
) -> SourceRegistryEntry:
    actor.require(Permission.REGISTRY_WRITE)
    why = _reason(reason)
    entry = await _load(session, entry_id, lock=True)
    if entry.version != expected_version:
        raise Conflict(
            "Registry entry changed meanwhile",
            code=ec.ATL_REGISTRY_CONFLICT,
            current_version=entry.version,
        )
    if not entry.active:
        raise Conflict("Registry entry is already inactive", code=ec.ATL_REGISTRY_CONFLICT)
    entry.active = False
    entry.version += 1
    entry.updated_by = actor.label
    entry.updated_at = _now()
    await _record(session, entry, RegistryChange.DEACTIVATED, why, actor.label)
    return entry


async def apply_control_seed(
    session: AsyncSession,
    actor: Actor,
    entry_id: int,
    *,
    controlled_by: str | None,
    controlled_by_qid: str | None,
    aliases: Sequence[str],
) -> bool:
    """Write the Wikidata controller; never overwrites a team or official value.

    Returns whether the row was written."""
    actor.require(Permission.REGISTRY_WRITE)
    entry = await _load(session, entry_id, lock=True)
    if entry.control_origin not in (None, ControlOrigin.WIKIDATA.value):
        return False
    control = _text(controlled_by, "controlled_by", required=False)
    qid = _qid(controlled_by_qid, "controlled_by_qid") if control else None
    names_ = _aliases(aliases, "controlled_by_aliases") if control else []
    if (
        entry.controlled_by == control
        and entry.controlled_by_qid == qid
        and list(entry.controlled_by_aliases or []) == names_
        and (entry.control_origin == ControlOrigin.WIKIDATA.value or control is None)
    ):
        return False
    entry.controlled_by = control
    entry.controlled_by_qid = qid
    entry.controlled_by_aliases = names_
    entry.control_origin = ControlOrigin.WIKIDATA.value if control else None
    entry.version += 1
    entry.updated_by = actor.label
    entry.updated_at = _now()
    await _record(
        session, entry, RegistryChange.UPDATED, "controlled_by seeded from Wikidata", actor.label
    )
    return True


async def request_control_seed(session: AsyncSession, actor: Actor, ref: str | int) -> bool:
    """Enqueue the Wikidata control seed of an entry (deduplicated); no event."""
    actor.require(Permission.REGISTRY_WRITE)
    entry = await _load(session, ref)
    job = await enqueue(
        session,
        names.ATLAS_QUEUE,
        names.JOB_CONTROL_SEED,
        {"registry_id": str(entry.id)},
        dedupe_key=names.control_seed_key(entry.id),
    )
    return job is not None


# --- country registries ---------------------------------------------------------------


def _http_url(value: object, field: str) -> str:
    text = _text(value, field) or ""
    if not re.match(r"^https?://[^\s/]+", text) or len(text) > 2000:
        raise _invalid("An http(s) URL is required", field=field)
    return text


def _enum(value: object, enum: type[Any], field: str) -> str:
    allowed = [m.value for m in enum]
    if value not in allowed:
        raise _invalid(f"Unknown {field}", field=field, allowed=allowed)
    return str(value)


async def _country_write(
    session: AsyncSession,
    actor: Actor,
    table: str,
    model: Any,
    where: Sequence[Any],
    values: dict[str, Any],
    expected_version: int | None,
) -> Any:
    existing = await session.scalar(select(model).where(*where).with_for_update())
    if existing is None:
        row = model(**values, updated_by=actor.label, version=1)
        session.add(row)
        change = "created"
    else:
        row = existing
        if expected_version is not None and row.version != expected_version:
            raise Conflict(
                "Registry row changed meanwhile",
                code=ec.ATL_REGISTRY_CONFLICT,
                current_version=row.version,
            )
        changed = {k: v for k, v in values.items() if getattr(row, k) != v}
        if not changed:
            return row
        for key, value in changed.items():
            setattr(row, key, value)
        row.version += 1
        row.updated_by = actor.label
        row.updated_at = _now()
        change = "updated"
    await session.flush()
    await session.refresh(row)
    await publish(
        session,
        names.COUNTRY_REGISTRY_CHANGED,
        {"table": table, "id": str(row.id), "country": row.country, "change": change},
        aggregate_type=table,
        aggregate_id=row.id,
        actor=actor.label,
    )
    return row


async def upsert_jurisdiction_source(
    session: AsyncSession,
    actor: Actor,
    *,
    country: str,
    source_type: str,
    key: str,
    name: str,
    tier: str = "primary",
    adapter: str | None = None,
    domain: str | None = None,
    notes: str | None = None,
    active: bool = True,
    expected_version: int | None = None,
) -> JurisdictionSource:
    actor.require(Permission.REGISTRY_WRITE)
    ctry = _country(country, required=True)
    stype = _enum(source_type, JurisdictionSourceType, "source_type")
    key_ = str(_text(key, "key"))
    adapter_ = _text(adapter, "adapter", required=False)
    domain_ = normalize_domain(domain) if domain else None
    if adapter_ is None and domain_ is None:
        raise _invalid("An adapter or a domain is required", field="adapter")
    values = {
        "country": ctry,
        "source_type": stype,
        "key": key_,
        "adapter": adapter_,
        "domain": domain_,
        "name": str(_text(name, "name")),
        "tier": _tier(tier),
        "notes": _text(notes, "notes", required=False),
        "active": bool(active),
    }
    where = [
        JurisdictionSource.country == ctry,
        JurisdictionSource.source_type == stype,
        JurisdictionSource.key == key_,
    ]
    row: JurisdictionSource = await _country_write(
        session, actor, "jurisdiction_sources", JurisdictionSource, where, values, expected_version
    )
    return row


async def upsert_legal_portal(
    session: AsyncSession,
    actor: Actor,
    *,
    country: str,
    name: str,
    url: str,
    access: str,
    eli: bool = False,
    adapter: str | None = None,
    notes: str | None = None,
    active: bool = True,
    expected_version: int | None = None,
) -> LegalPortal:
    actor.require(Permission.REGISTRY_WRITE)
    ctry = _country(country, required=True)
    url_ = _http_url(url, "url")
    values = {
        "country": ctry,
        "name": str(_text(name, "name")),
        "url": url_,
        "domain": normalize_domain(url_),
        "eli": bool(eli),
        "access": _enum(access, LegalAccess, "access"),
        "adapter": _text(adapter, "adapter", required=False),
        "notes": _text(notes, "notes", required=False),
        "active": bool(active),
    }
    where = [LegalPortal.country == ctry, LegalPortal.url == url_]
    row: LegalPortal = await _country_write(
        session, actor, "legal_portals", LegalPortal, where, values, expected_version
    )
    return row


async def upsert_election(
    session: AsyncSession,
    actor: Actor,
    *,
    country: str,
    kind: str,
    name: str,
    first_round: date,
    second_round: date | None = None,
    source_url: str | None = None,
    notes: str | None = None,
    expected_version: int | None = None,
) -> ElectionEntry:
    actor.require(Permission.REGISTRY_WRITE)
    ctry = _country(country, required=True)
    kind_ = _enum(kind, ElectionKind, "kind")
    if second_round is not None and second_round < first_round:
        raise _invalid("second_round precedes first_round", field="second_round")
    values = {
        "country": ctry,
        "kind": kind_,
        "name": str(_text(name, "name")),
        "first_round": first_round,
        "second_round": second_round,
        "source_url": _http_url(source_url, "source_url") if source_url else None,
        "notes": _text(notes, "notes", required=False),
    }
    where = [
        ElectionEntry.country == ctry,
        ElectionEntry.kind == kind_,
        ElectionEntry.first_round == first_round,
    ]
    row: ElectionEntry = await _country_write(
        session, actor, "election_calendar", ElectionEntry, where, values, expected_version
    )
    return row


async def list_jurisdiction_sources(
    session: AsyncSession, actor: Actor, country: str, *, source_type: str | None = None
) -> list[JurisdictionSource]:
    actor.require(Permission.REGISTRY_READ)
    stmt = select(JurisdictionSource).where(
        JurisdictionSource.country == str(_country(country, required=True))
    )
    if source_type:
        stmt = stmt.where(JurisdictionSource.source_type == source_type)
    rows = await session.scalars(
        stmt.order_by(JurisdictionSource.source_type, JurisdictionSource.key)
    )
    return list(rows)


async def list_legal_portals(
    session: AsyncSession, actor: Actor, country: str
) -> list[LegalPortal]:
    actor.require(Permission.REGISTRY_READ)
    rows = await session.scalars(
        select(LegalPortal)
        .where(LegalPortal.country == str(_country(country, required=True)))
        .order_by(LegalPortal.name)
    )
    return list(rows)


async def list_elections(
    session: AsyncSession, actor: Actor, country: str, *, upcoming_from: date | None = None
) -> list[ElectionEntry]:
    actor.require(Permission.REGISTRY_READ)
    stmt = select(ElectionEntry).where(
        ElectionEntry.country == str(_country(country, required=True))
    )
    if upcoming_from is not None:
        stmt = stmt.where(
            func.coalesce(ElectionEntry.second_round, ElectionEntry.first_round) >= upcoming_from
        )
    rows = await session.scalars(stmt.order_by(ElectionEntry.first_round))
    return list(rows)
