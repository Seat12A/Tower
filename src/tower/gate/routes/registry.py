"""Reliability registry (versioned, publishable) and country registries."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tower.core.models.atlas import (
    ElectionEntry,
    JurisdictionSource,
    LegalPortal,
    SourceRegistryVersion,
)
from tower.core.models.factcheck import SourceRegistryEntry
from tower.core.usecases import registry as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(tags=["registry"])


class EntryOut(BaseModel):
    id: str
    domain: str
    publisher: str
    tier: str
    kind: str
    country: str | None
    reason: str | None
    origin: str
    version: int
    aliases: list[str]
    qid: str | None
    controlled_by: str | None
    controlled_by_qid: str | None
    controlled_by_aliases: list[str]
    control_origin: str | None
    statutory_independence: bool
    independence_basis: str | None
    active: bool
    updated_by: str | None
    updated_at: str | None
    reviewed_at: str | None


class VersionOut(BaseModel):
    version: int
    change: str
    reason: str | None
    changed_by: str
    created_at: str
    snapshot: dict[str, Any]


class ExportOut(BaseModel):
    generated_at: str
    count: int
    digest: str
    entries: list[dict[str, Any]]


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(max_length=300)
    publisher: str = Field(max_length=500)
    tier: str = Field(max_length=16)
    kind: str = Field(max_length=32)
    country: str | None = Field(default=None, max_length=8)
    reason: str = Field(max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    qid: str | None = Field(default=None, max_length=32)
    controlled_by: str | None = Field(default=None, max_length=500)
    controlled_by_qid: str | None = Field(default=None, max_length=32)
    controlled_by_aliases: list[str] = Field(default_factory=list, max_length=50)
    statutory_independence: bool = False
    independence_basis: str | None = Field(default=None, max_length=32)


class EntryPatch(BaseModel):
    """Only the fields present in the body change (``null`` clears where allowed)."""

    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    reason: str = Field(max_length=2000)
    publisher: str | None = Field(default=None, max_length=500)
    tier: str | None = Field(default=None, max_length=16)
    kind: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=8)
    aliases: list[str] | None = Field(default=None, max_length=50)
    qid: str | None = Field(default=None, max_length=32)
    controlled_by: str | None = Field(default=None, max_length=500)
    controlled_by_qid: str | None = Field(default=None, max_length=32)
    controlled_by_aliases: list[str] | None = Field(default=None, max_length=50)
    statutory_independence: bool | None = None
    independence_basis: str | None = Field(default=None, max_length=32)


class DeactivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    reason: str = Field(max_length=2000)


class SeedControlOut(BaseModel):
    enqueued: bool


def _entry(row: SourceRegistryEntry) -> EntryOut:
    return EntryOut.model_validate(uc.entry_snapshot(row))


def _version(row: SourceRegistryVersion) -> VersionOut:
    return VersionOut(
        version=row.version,
        change=row.change,
        reason=row.reason,
        changed_by=row.changed_by,
        created_at=row.created_at.isoformat(),
        snapshot=row.snapshot,
    )


@router.get("/v1/registry", response_model=list[EntryOut])
async def list_entries(
    actor: ActorDep,
    session: ReadSession,
    country: Annotated[str | None, Query(max_length=8)] = None,
    tier: Annotated[str | None, Query(max_length=16)] = None,
    active: bool | None = True,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EntryOut]:
    rows = await uc.list_registry(
        session, actor, country=country, tier=tier, active=active, q=q, limit=limit, offset=offset
    )
    return [_entry(r) for r in rows]


@router.get("/v1/registry/export", response_model=ExportOut)
async def export(actor: ActorDep, session: ReadSession) -> ExportOut:
    """Published registry: every active entry, with a content digest."""
    return ExportOut.model_validate(await uc.export_registry(session, actor))


@router.post("/v1/registry", response_model=EntryOut, status_code=201)
async def create_entry(body: EntryIn, actor: ActorDep, session: WriteSession) -> EntryOut:
    return _entry(await uc.create_entry(session, actor, **body.model_dump()))


@router.get("/v1/registry/{ref}", response_model=EntryOut)
async def get_entry(ref: str, actor: ActorDep, session: ReadSession) -> EntryOut:
    """By numeric id or by domain."""
    return _entry(await uc.get_entry(session, actor, ref))


@router.get("/v1/registry/{ref}/history", response_model=list[VersionOut])
async def history(ref: str, actor: ActorDep, session: ReadSession) -> list[VersionOut]:
    return [_version(r) for r in await uc.entry_history(session, actor, ref)]


@router.patch("/v1/registry/{ref}", response_model=EntryOut)
async def update_entry(
    ref: str, body: EntryPatch, actor: ActorDep, session: WriteSession
) -> EntryOut:
    changes = body.model_dump(exclude_unset=True, exclude={"version", "reason"})
    row = await uc.update_entry(
        session,
        actor,
        ref,
        expected_version=body.version,
        changes=changes,
        reason=body.reason,
    )
    return _entry(row)


@router.post("/v1/registry/{ref}/deactivate", response_model=EntryOut)
async def deactivate_entry(
    ref: str, body: DeactivateIn, actor: ActorDep, session: WriteSession
) -> EntryOut:
    row = await uc.deactivate_entry(
        session, actor, ref, expected_version=body.version, reason=body.reason
    )
    return _entry(row)


@router.post("/v1/registry/{ref}/seed-control", response_model=SeedControlOut, status_code=202)
async def seed_control(ref: str, actor: ActorDep, session: WriteSession) -> SeedControlOut:
    """Enqueue the Wikidata seeding of ``controlled_by`` (never overwrites a team value)."""
    return SeedControlOut(enqueued=await uc.request_control_seed(session, actor, ref))


# --- country registries -----------------------------------------------------------------


class SourceOut(BaseModel):
    id: str
    country: str
    source_type: str
    key: str
    adapter: str | None
    domain: str | None
    name: str
    tier: str
    notes: str | None
    active: bool
    version: int
    updated_by: str | None


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: str = Field(max_length=32)
    key: str = Field(max_length=100)
    name: str = Field(max_length=500)
    tier: str = Field(default="primary", max_length=16)
    adapter: str | None = Field(default=None, max_length=64)
    domain: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=500)
    active: bool = True
    version: int | None = Field(default=None, ge=1)


class PortalOut(BaseModel):
    id: str
    country: str
    name: str
    url: str
    domain: str
    eli: bool
    access: str
    adapter: str | None
    notes: str | None
    active: bool
    version: int
    updated_by: str | None


class PortalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=500)
    url: str = Field(max_length=2000)
    access: str = Field(max_length=16)
    eli: bool = False
    adapter: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=500)
    active: bool = True
    version: int | None = Field(default=None, ge=1)


class ElectionOut(BaseModel):
    id: str
    country: str
    kind: str
    name: str
    first_round: date
    second_round: date | None
    source_url: str | None
    notes: str | None
    version: int
    updated_by: str | None


class ElectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=16)
    name: str = Field(max_length=500)
    first_round: date
    second_round: date | None = None
    source_url: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=500)
    version: int | None = Field(default=None, ge=1)


def _source(row: JurisdictionSource) -> SourceOut:
    return SourceOut(**{**_cols(row, SourceOut), "id": str(row.id)})


def _portal(row: LegalPortal) -> PortalOut:
    return PortalOut(**{**_cols(row, PortalOut), "id": str(row.id)})


def _election(row: ElectionEntry) -> ElectionOut:
    return ElectionOut(**{**_cols(row, ElectionOut), "id": str(row.id)})


def _cols(row: object, model: type[BaseModel]) -> dict[str, Any]:
    return {k: getattr(row, k) for k in model.model_fields if k != "id"}


@router.get("/v1/countries/{country}/sources", response_model=list[SourceOut])
async def list_sources(
    country: str,
    actor: ActorDep,
    session: ReadSession,
    source_type: Annotated[str | None, Query(max_length=32)] = None,
) -> list[SourceOut]:
    rows = await uc.list_jurisdiction_sources(session, actor, country, source_type=source_type)
    return [_source(r) for r in rows]


@router.put("/v1/countries/{country}/sources", response_model=SourceOut)
async def put_source(
    country: str, body: SourceIn, actor: ActorDep, session: WriteSession
) -> SourceOut:
    """Upsert one source of the country, identified by ``(source_type, key)``."""
    data = body.model_dump()
    row = await uc.upsert_jurisdiction_source(
        session, actor, country=country, expected_version=data.pop("version"), **data
    )
    return _source(row)


@router.get("/v1/countries/{country}/legal-portals", response_model=list[PortalOut])
async def list_portals(country: str, actor: ActorDep, session: ReadSession) -> list[PortalOut]:
    return [_portal(r) for r in await uc.list_legal_portals(session, actor, country)]


@router.put("/v1/countries/{country}/legal-portals", response_model=PortalOut)
async def put_portal(
    country: str, body: PortalIn, actor: ActorDep, session: WriteSession
) -> PortalOut:
    """Upsert one legal portal of the country, identified by its URL."""
    data = body.model_dump()
    row = await uc.upsert_legal_portal(
        session, actor, country=country, expected_version=data.pop("version"), **data
    )
    return _portal(row)


@router.get("/v1/countries/{country}/elections", response_model=list[ElectionOut])
async def list_country_elections(
    country: str,
    actor: ActorDep,
    session: ReadSession,
    upcoming_from: date | None = None,
) -> list[ElectionOut]:
    rows = await uc.list_elections(session, actor, country, upcoming_from=upcoming_from)
    return [_election(r) for r in rows]


@router.put("/v1/countries/{country}/elections", response_model=ElectionOut)
async def put_election(
    country: str, body: ElectionIn, actor: ActorDep, session: WriteSession
) -> ElectionOut:
    """Upsert one election, identified by ``(kind, first_round)``."""
    data = body.model_dump()
    row = await uc.upsert_election(
        session, actor, country=country, expected_version=data.pop("version"), **data
    )
    return _election(row)
