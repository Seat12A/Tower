"""HTTP schemas. X and Discord IDs are always strings (JavaScript precision)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.cases import CaseKind, CaseStatus
from tower.core.domain.factcheck.values import ReviewDecision, ReviewOutcome
from tower.core.models.cases import Case, CaseEvent
from tower.core.usecases.drafts import DraftView, PublicVerdict


class CaseOut(BaseModel):
    public_id: uuid.UUID
    kind: CaseKind
    status: CaseStatus
    x_post_id: str | None
    title: str | None
    priority: int
    assigned_to: str | None
    published_url: str | None
    discord_thread_id: str | None = None
    """Discord forum post showing the case (string, like every Discord id)."""
    draft_id: uuid.UUID | None = None
    """Latest fact-check draft of the case: only on ``GET /v1/cases/{id}``."""
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, d: Case, *, draft_id: uuid.UUID | None = None) -> CaseOut:
        return cls(
            public_id=d.public_id,
            kind=CaseKind(d.kind),
            status=CaseStatus(d.status),
            x_post_id=None if d.x_post_id is None else str(d.x_post_id),
            title=d.title,
            priority=d.priority,
            assigned_to=None if d.assigned_to is None else str(d.assigned_to),
            published_url=d.published_url,
            discord_thread_id=None if d.discord_thread_id is None else str(d.discord_thread_id),
            draft_id=draft_id,
            version=d.version,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class CasePublicOut(BaseModel):
    """Public website view: published cases only, no internal data."""

    public_id: uuid.UUID
    kind: CaseKind
    x_post_id: str | None
    title: str | None
    published_url: str | None
    updated_at: datetime


class CaseEventOut(BaseModel):
    from_status: CaseStatus | None
    to_status: CaseStatus
    actor: str
    reason: str | None
    created_at: datetime

    @classmethod
    def of(cls, ev: CaseEvent) -> CaseEventOut:
        return cls(
            from_status=None if ev.from_status is None else CaseStatus(ev.from_status),
            to_status=CaseStatus(ev.to_status),
            actor=ev.actor,
            reason=ev.reason,
            created_at=ev.created_at,
        )


class DiscordThreadIn(BaseModel):
    thread_id: str = Field(pattern=r"^[0-9]{1,20}$", description="Discord thread id")


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: CaseKind
    x_post_id: str | None = Field(default=None, pattern=r"^\d{1,20}$")
    title: str | None = Field(default=None, max_length=300)
    priority: int = Field(default=0, ge=-100, le=100)
    reason: str | None = Field(default=None, max_length=1000)


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: CaseStatus
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


class ManualNoteIn(BaseModel):
    """A claim, a verdict and a context note written by the team (no model).
    Give a content URL, or a platform and an external id."""

    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, max_length=2048)
    platform: str | None = Field(default=None, max_length=32)
    external_id: str | None = Field(default=None, max_length=256)
    claim_text: str = Field(max_length=1000)
    """In French, 300 characters at most."""
    claim_type: str = Field(max_length=40)
    veracity: str = Field(max_length=40)
    context_need: str | None = Field(default=None, max_length=40)
    context_types: list[Annotated[str, Field(max_length=40)]] = Field(
        default_factory=list, max_length=15
    )
    note_text: str | None = Field(default=None, max_length=4000)
    note_lang: str = Field(default="fr", max_length=8)
    links: list[Annotated[str, Field(max_length=2048)]] = Field(default_factory=list, max_length=20)
    fact_sheet: str | None = Field(default=None, max_length=8000)


class ManualNoteOut(BaseModel):
    platform: str
    external_id: str
    status: Literal["drafted", "queued"]
    """``drafted``: the draft is open (``draft_id``); ``queued``: the post is read first."""
    draft_id: str | None
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


class DraftCaseOut(BaseModel):
    id: str
    status: str
    discord_thread_id: str | None = None


class DraftNoteOut(BaseModel):
    text: str
    fact_sheet: str | None
    origin: str


class DraftGroupOut(BaseModel):
    id: str
    level: str
    members: int


class DraftPostOut(BaseModel):
    platform: str
    external_id: str
    url: str | None
    posted_at: str | None


class DraftReusableOut(BaseModel):
    id: str
    veracity: str
    group_id: str


class DraftFactOut(BaseModel):
    indicator: str
    label: str
    value: str | None
    unit: str
    period_start: str
    period_end: str
    territory: str
    status: str


class DraftBandOut(BaseModel):
    band: str
    side: str
    label: str


class DraftNarrativeOut(BaseModel):
    id: str
    title: str | None
    state: str
    groups: int


class DraftCoordinationOut(BaseModel):
    """Informative signal; never a verdict input."""

    kind: str
    count: int
    window_minutes: int
    detail: dict[str, Any]


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
    case: DraftCaseOut | None
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
    group: DraftGroupOut | None = None
    other_posts: list[DraftPostOut] = []
    platforms: list[str] = []
    reusable_verdict: DraftReusableOut | None = None
    reference_fact: DraftFactOut | None = None
    band: DraftBandOut | None = None
    outdated: bool = False
    external_factchecks: list[dict[str, str]] = []
    narrative: DraftNarrativeOut | None = None
    coordination: list[DraftCoordinationOut] = []

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
    validate_band: bool = False
    """With a validation: also validate the value band of the group (editors)."""


class ReviewOut(BaseModel):
    outcome: ReviewOutcome
    draft: DraftOut


class PublicNoteOut(BaseModel):
    lang: str
    text: str
    fact_sheet: str | None
    machine_translated: bool
    links: list[str]


class PublicInheritedOut(BaseModel):
    """The verdict covers this post through another one (identical signature or band)."""

    rule: str
    origin_verdict_id: str
    origin_url: str | None


class PublicClaimOut(BaseModel):
    text: str
    lang: str
    veracity: str
    context_need: str
    context_types: list[str]
    validated_at: datetime | None
    note: PublicNoteOut | None
    inherited: PublicInheritedOut | None = None


class VerdictBucketOut(BaseModel):
    platform: str
    bucket: str
    hashes: list[str]
    """Full sha256 (hex) of the native ids of the bucket's posts with a visible verdict."""


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
