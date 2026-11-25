"""HTTP schemas. X and Discord IDs are always strings (JavaScript precision)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.cases import CaseKind, CaseStatus
from tower.core.domain.factcheck.values import ReviewDecision, ReviewOutcome
from tower.core.models.cases import Case, CaseEvent
from tower.core.usecases.case_views import CaseDetails
from tower.core.usecases.drafts import DraftView, PublicVerdict

CaseId = Annotated[
    str,
    Path(
        min_length=8,
        max_length=36,
        description="Short id of the case (8 hex characters or more); the full public id "
        "is still accepted during the transition.",
        examples=["fbaa8363"],
    ),
]


class CaseOut(BaseModel):
    short_id: str
    """Identifier to show and link everywhere (``/v1/cases/{short_id}``)."""
    public_id: uuid.UUID
    """Full id, kept during the transition to short ids (contract: remove)."""
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
            short_id=d.short_id,
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

    short_id: str
    public_id: uuid.UUID
    kind: CaseKind
    x_post_id: str | None
    title: str | None
    published_url: str | None
    updated_at: datetime


class CaseSummaryOut(BaseModel):
    """One case of a search result. Internal fields (``status``, ``priority``,
    ``assigned_to``) are ``null`` for the public, who only sees published cases."""

    short_id: str
    public_id: uuid.UUID
    kind: CaseKind
    title: str | None
    platform: str | None
    x_post_id: str | None
    external_id: str | None
    url: str | None
    published_url: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    claim_type: str | None
    claim_lang: str | None
    jurisdictions: list[str]
    veracity: str | None
    context_need: str | None
    verdict_state: str | None
    """``current``, ``expired`` or ``superseded`` (team: also ``draft``, ``rejected``)."""
    status: CaseStatus | None = None
    priority: int | None = None
    assigned_to: str | None = None


class CasePageOut(BaseModel):
    items: list[CaseSummaryOut]
    page: int
    per_page: int
    total: int
    pages: int


class CaseContentOut(BaseModel):
    platform: str
    external_id: str
    url: str | None
    lang: str


class CaseClaimOut(BaseModel):
    text: str
    """In the requested language when it is French, otherwise in the post language."""
    lang: str
    text_fr: str
    claim_type: str
    subject: str | None
    place: str | None
    period_start: date | None
    period_end: date | None
    jurisdictions: list[str]


class CaseVerdictOut(BaseModel):
    id: uuid.UUID
    veracity: str
    context_need: str
    context_types: list[str]
    validated_at: datetime | None
    expires_at: datetime | None
    state: str | None


class CaseTimelineOut(BaseModel):
    status: CaseStatus
    at: datetime
    from_status: CaseStatus | None = None
    actor: str | None = None
    reason: str | None = None


class CaseInternalOut(BaseModel):
    discord_thread_id: str | None
    draft_id: uuid.UUID | None
    verdict_status: str | None
    confidence: float | None
    level: str | None
    version: int
    opened_by: str


class CaseDetailsOut(CaseSummaryOut):
    """Everything about a case: post, claim, verdict, note in the best language, number
    of posts carrying the claim, status timeline (``internal``: team only)."""

    content: CaseContentOut | None
    claim: CaseClaimOut | None
    verdict: CaseVerdictOut | None
    note: PublicNoteOut | None
    posts_count: int
    timeline: list[CaseTimelineOut]
    internal: CaseInternalOut | None = None

    @classmethod
    def of(cls, d: CaseDetails) -> CaseDetailsOut:
        return cls.model_validate(
            asdict(d.summary)
            | {
                "content": d.content,
                "claim": d.claim,
                "verdict": d.verdict,
                "note": None if d.note is None else asdict(d.note),
                "posts_count": d.posts_count,
                "timeline": d.timeline,
                "internal": d.internal,
            }
        )


class CaseStatsOut(BaseModel):
    """Counts of the matching cases (public: published cases, validated verdicts)."""

    total: int
    by_kind: dict[str, int]
    by_veracity: dict[str, int]
    by_claim_type: dict[str, int]
    by_lang: dict[str, int]
    by_jurisdiction: dict[str, int]
    by_status: dict[str, int] | None
    interval: str
    timeline: list[dict[str, Any]]
    """``[{period: "YYYY-MM-DD" (start of the period), count}]``, oldest first."""
    generated_at: datetime


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


class PendingRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step: Literal["reading", "extracting"]
    status: Literal["queued", "running"]
    level: str | None
    platform: str | None
    external_id: str | None
    url: str | None
    requested_by: str
    attempts: int
    last_error: str | None
    created_at: datetime


class RunningInvestigationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    group_id: str
    level: str
    depth: str
    stage: str
    round: int
    cost_usd: float
    claim: str | None
    platform: str | None
    external_id: str | None
    url: str | None
    requested_by: str
    error: str | None
    created_at: datetime
    updated_at: datetime


class FactcheckInProgressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    requests: list[PendingRequestOut]
    investigations: list[RunningInvestigationOut]


class DraftCaseOut(BaseModel):
    id: str
    short_id: str | None = None
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
