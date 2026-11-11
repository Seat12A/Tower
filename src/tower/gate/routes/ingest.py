"""Content ingestion (extension and services) and server-driven extension filters."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput
from tower.core.domain.ingestion import AuthorInput, ItemInput, MediaInput
from tower.core.domain.privacy import validate_installation_token
from tower.core.domain.security import Actor
from tower.core.usecases import ingestion as uc
from tower.gate.deps import ReadSession, WriteSession, current_actor
from tower.gate.ratelimit import client_ip

router = APIRouter(prefix="/v1/ingest", tags=["ingestion"])

MAX_BODY_BYTES = 256 * 1024
CONFIG_CACHE_CONTROL = "public, max-age=300"


class AuthorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_id: str | None = Field(default=None, max_length=512)
    handle: str | None = Field(default=None, max_length=512)


class MediaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(max_length=32)
    url: str


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str = Field(max_length=32)
    external_id: str = Field(max_length=512)
    text: str
    author: AuthorIn | None = None
    lang_hint: str | None = Field(default=None, max_length=32)
    created_at: datetime | None = None
    metrics: dict[Annotated[str, Field(max_length=32)], Annotated[int, Field(ge=0, le=2**62)]] = (
        Field(default_factory=dict, max_length=32)
    )
    parent_external_id: str | None = Field(default=None, max_length=512)
    quoted_external_id: str | None = Field(default=None, max_length=512)
    media: list[MediaIn] = Field(default_factory=list)
    observed_at: datetime | None = None

    def to_domain(self) -> ItemInput:
        author = None
        if self.author is not None:
            author = AuthorInput(self.author.external_id, self.author.handle)
        return ItemInput(
            platform=self.platform,
            external_id=self.external_id,
            text=self.text,
            author=author,
            lang_hint=self.lang_hint,
            created_at=self.created_at,
            metrics=self.metrics,
            parent_external_id=self.parent_external_id,
            quoted_external_id=self.quoted_external_id,
            media=tuple(MediaInput(m.kind, m.url) for m in self.media),
            observed_at=self.observed_at,
        )


class IngestBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter_version: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=16)
    items: list[ItemIn]


class ItemErrorOut(BaseModel):
    index: int
    code: str


class IngestOut(BaseModel):
    accepted: int
    new: int
    duplicates: int
    rejected: int
    errors: list[ItemErrorOut]


class FiltersOut(BaseModel):
    version: str | None
    filters: dict[str, Any]


def rate_limit(request: Request) -> None:
    """Per-IP token bucket, evaluated before authentication and body reading."""
    if not request.app.state.limiter.allow(client_ip(request)):
        raise InvalidInput("Too many ingestion requests", code=ec.ING_RATE_LIMITED)


async def ingest_actor(
    request: Request,
    x_installation_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    x_acting_discord_id: Annotated[str | None, Header()] = None,
) -> tuple[Actor, str | None]:
    """Installation token (extension) or regular actor (service token)."""
    if x_installation_token is not None:
        return Actor.installation(), validate_installation_token(x_installation_token)
    return await current_actor(request, authorization, x_acting_discord_id), None


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise InvalidInput("Request body too large", code=ec.ING_BODY_TOO_LARGE)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise InvalidInput("Request body too large", code=ec.ING_BODY_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/observations",
    response_model=IngestOut,
    status_code=202,
    dependencies=[Depends(rate_limit)],
)
async def ingest_observations(
    request: Request,
    session: WriteSession,
    auth: Annotated[tuple[Actor, str | None], Depends(ingest_actor)],
) -> IngestOut:
    actor, token = auth
    body = await _read_body(request)
    try:
        batch = IngestBatch.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(
            exc.errors(include_url=False, include_context=False, include_input=False)
        ) from None
    settings = request.app.state.settings
    result = await uc.ingest_items(
        session,
        actor,
        [item.to_domain() for item in batch.items],
        source=batch.source or "radar",
        filter_version=batch.filter_version,
        installation_token=token,
        max_items=settings.ingest_max_items,
        daily_quota=settings.ingest_daily_quota,
    )
    return IngestOut(
        accepted=result.accepted,
        new=result.new,
        duplicates=result.duplicates,
        rejected=result.rejected,
        errors=[ItemErrorOut(index=i, code=c) for i, c in result.errors],
    )


def _etag(version: str | None, filters: dict[str, Any]) -> str:
    canonical = orjson.dumps({"version": version, "filters": filters}, option=orjson.OPT_SORT_KEYS)
    return f'W/"{hashlib.sha256(canonical).hexdigest()[:16]}"'


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    wanted = etag.removeprefix("W/")
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == wanted:
            return True
    return False


@router.get("/config", response_model=FiltersOut)
async def ingest_config(
    session: ReadSession,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    version, filters = await uc.get_active_filters(session)
    etag = _etag(version, filters)
    headers = {"ETag": etag, "Cache-Control": CONFIG_CACHE_CONTROL}
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse({"version": version, "filters": filters}, headers=headers)
