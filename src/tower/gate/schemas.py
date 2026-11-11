"""HTTP schemas. X and Discord IDs are always strings (JavaScript precision)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.dossiers import DossierKind, DossierStatus
from tower.core.domain.factcheck.values import ReviewDecision, ReviewOutcome
from tower.core.models.dossiers import Dossier, DossierEvent
from tower.core.usecases.drafts import DraftView, PublicVerdict


class DossierOut(BaseModel):
    public_id: uuid.UUID
    kind: DossierKind
    status: DossierStatus
    x_post_id: str | None
    title: str | None
    priority: int
    assigned_to: str | None
    published_url: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, d: Dossier) -> DossierOut:
        return cls(
            public_id=d.public_id,
            kind=DossierKind(d.kind),
            status=DossierStatus(d.status),
            x_post_id=None if d.x_post_id is None else str(d.x_post_id),
            title=d.title,
            priority=d.priority,
            assigned_to=None if d.assigned_to is None else str(d.assigned_to),
            published_url=d.published_url,
            version=d.version,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class DossierPublicOut(BaseModel):
    """Public website view: published dossiers only, no internal data."""

    public_id: uuid.UUID
    kind: DossierKind
    x_post_id: str | None
    title: str | None
    published_url: str | None
    updated_at: datetime


class DossierEventOut(BaseModel):
    from_status: DossierStatus | None
    to_status: DossierStatus
    actor: str
    reason: str | None
    created_at: datetime

    @classmethod
    def of(cls, ev: DossierEvent) -> DossierEventOut:
        return cls(
            from_status=None if ev.from_status is None else DossierStatus(ev.from_status),
            to_status=DossierStatus(ev.to_status),
            actor=ev.actor,
            reason=ev.reason,
            created_at=ev.created_at,
        )


class DossierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: DossierKind
    x_post_id: str | None = Field(default=None, pattern=r"^\d{1,20}$")
    title: str | None = Field(default=None, max_length=300)
    priority: int = Field(default=0, ge=-100, le=100)
    reason: str | None = Field(default=None, max_length=1000)


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: DossierStatus
    reason: str | None = Field(default=None, max_length=1000)
    expected_version: int | None = None
    published_url: str | None = Field(default=None, max_length=300)


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=300)
    expected_version: int | None = None


# --- Fact-check drafts, manual requests and the public verdict -----------------------


class FactcheckRequestIn(BaseModel):
    """Give a content URL, or a platform and an external id."""

    model_config = ConfigDict(extra="forbid")
    platform: str | None = Field(default=None, max_length=32)
    external_id: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=2048)


class FactcheckRequestOut(BaseModel):
    platform: str
    external_id: str
    accepted: bool
    duplicate: bool


class DraftSourceOut(BaseModel):
    url: str
    publisher: str | None
    title: str | None
    tier: str


class DraftCitationOut(BaseModel):
    label: str | None
    quote: str
    valid: bool
    lang: str | None
    translation_fr: str | None
    source: DraftSourceOut | None


class DraftRationaleOut(BaseModel):
    assertion: str
    citations: list[DraftCitationOut]


class DraftClaimOut(BaseModel):
    text_original: str | None = None
    lang: str | None = None
    text_fr: str | None = None


class DraftContentOut(BaseModel):
    platform: str
    external_id: str
    url: str | None
    lang: str


class DraftDossierOut(BaseModel):
    id: str
    status: str


class DraftNoteOut(BaseModel):
    text: str
    fact_sheet: str | None
    origin: str


class DraftOut(BaseModel):
    """A fact-check draft as shown to reviewers (``draft_id`` = public verdict id)."""

    draft_id: str
    seq: int
    version: int
    status: str
    level: str
    depth: str | None
    veracity: str
    context_need: str
    context_types: list[str]
    confidence: float | None
    confidence_band: str | None
    claim: DraftClaimOut
    content: DraftContentOut | None
    dossier: DraftDossierOut | None
    notes: dict[str, DraftNoteOut]
    links: list[str]
    rationale: list[DraftRationaleOut]
    not_in_sources: list[str]
    review_points: list[str]
    objection: dict[str, Any] | None
    critic: dict[str, Any] | None
    checks: dict[str, Any]
    flags: dict[str, Any]
    sources: list[DraftSourceOut]
    drafted_at: datetime | None
    notified_at: datetime | None

    @classmethod
    def of(cls, view: DraftView) -> DraftOut:
        return cls.model_validate(asdict(view))


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: ReviewDecision
    veracity: str | None = Field(default=None, max_length=32)
    note_text: str | None = Field(default=None, max_length=2000)
    note_lang: str | None = Field(default=None, max_length=16)
    labels: list[str] = Field(default_factory=list, max_length=16)
    reason: str | None = Field(default=None, max_length=1000)
    expected_version: int | None = None


class ReviewOut(BaseModel):
    outcome: ReviewOutcome
    draft: DraftOut


class PublicNoteOut(BaseModel):
    lang: str
    text: str
    fact_sheet: str | None
    machine_translated: bool
    links: list[str]


class PublicClaimOut(BaseModel):
    text: str
    lang: str
    veracity: str
    context_need: str
    context_types: list[str]
    validated_at: datetime | None
    note: PublicNoteOut | None


class PublicVerdictOut(BaseModel):
    """Validated verdicts of a content, for the website and the extension."""

    platform: str
    external_id: str
    url: str | None
    lang: str
    claims: list[PublicClaimOut]

    @classmethod
    def of(cls, verdict: PublicVerdict) -> PublicVerdictOut:
        return cls.model_validate(asdict(verdict))
