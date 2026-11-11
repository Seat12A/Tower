"""Fact-check drafts: review queue, human decision, manual requests, public verdict.

Every write checks the permission, applies the domain rules, writes an audit row and
publishes its event in the same transaction (see docs/fact-check.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import get_settings
from tower.core.domain import error_codes as ec
from tower.core.domain.dossiers import DossierKind, DossierStatus, allowed_transitions
from tower.core.domain.errors import Conflict, InvalidInput, NotFound
from tower.core.domain.factcheck.checks import NOTE_MAX_CHARS, note_length
from tower.core.domain.factcheck.expiry import parse_ttl_overrides, verdict_expiry
from tower.core.domain.factcheck.urls import content_url, parse_content_url
from tower.core.domain.factcheck.values import (
    ClaimGroupState,
    ClaimType,
    FactStatus,
    NoteStatus,
    Regime,
    ReviewDecision,
    ReviewLabel,
    ReviewOutcome,
    TranslationOrigin,
    Veracity,
    VerdictStatus,
)
from tower.core.domain.platforms import Platform, parse_platform, validate_external_id
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.jobs.queue import enqueue
from tower.core.logging import get_logger
from tower.core.models.dossiers import Dossier
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    ContextNote,
    ContextNoteTranslation,
    DossierClaimGroup,
    EvidencePassage,
    Investigation,
    Review,
    Source,
    Verdict,
)
from tower.core.models.factcheck_v1 import ReferenceFact
from tower.core.models.ingestion import ContentItem
from tower.core.usecases import dossiers as dossier_uc
from tower.core.usecases import references as references_uc

__all__ = [
    "DraftView",
    "PublicClaim",
    "PublicNote",
    "PublicVerdict",
    "RequestResult",
    "ReviewResult",
    "get_draft",
    "get_public_verdict",
    "list_pending_drafts",
    "mark_draft_notified",
    "request_factcheck",
    "review_draft",
]

log = get_logger(__name__)

AGGREGATE = "verdict"
REFERENCE_LANGS = ("fr", "en")
"""Languages read by the team: a review validates (or edits) these versions."""
_NOTE_FLAG_KEYS = frozenset({"objection", "critic", "checks", "confidence_band"})
_MAX_LANG = 16


@dataclass(frozen=True, slots=True)
class DraftView:
    """Everything a reviewer needs about a draft (ids are public UUIDs as strings)."""

    draft_id: str
    seq: int
    """Internal id: the ``before_id`` cursor of the next page."""
    version: int
    status: str
    level: str
    depth: str | None
    veracity: str
    context_need: str
    context_types: list[str]
    confidence: float | None
    confidence_band: str | None
    claim: dict[str, Any]
    content: dict[str, Any] | None
    dossier: dict[str, Any] | None
    notes: dict[str, dict[str, Any]]
    links: list[str]
    rationale: list[dict[str, Any]]
    not_in_sources: list[str]
    review_points: list[str]
    objection: dict[str, Any] | None
    critic: dict[str, Any] | None
    checks: dict[str, Any]
    flags: dict[str, Any]
    sources: list[dict[str, Any]]
    drafted_at: datetime | None
    notified_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReviewResult:
    outcome: ReviewOutcome
    draft: DraftView


@dataclass(frozen=True, slots=True)
class RequestResult:
    platform: str
    external_id: str
    job_id: int | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class PublicNote:
    lang: str
    text: str
    fact_sheet: str | None
    machine_translated: bool
    links: list[str]


@dataclass(frozen=True, slots=True)
class PublicClaim:
    text: str
    lang: str
    veracity: str
    context_need: str
    context_types: list[str]
    validated_at: datetime | None
    note: PublicNote | None


@dataclass(frozen=True, slots=True)
class PublicVerdict:
    platform: str
    external_id: str
    url: str | None
    lang: str
    claims: list[PublicClaim] = field(default_factory=list)


# --- reading ---------------------------------------------------------------------------


async def _dossier_of_group(session: AsyncSession, group_id: int) -> Dossier | None:
    return await session.scalar(
        select(Dossier)
        .join(DossierClaimGroup, DossierClaimGroup.dossier_id == Dossier.id)
        .where(DossierClaimGroup.claim_group_id == group_id)
        .order_by(Dossier.id.desc())
        .limit(1)
    )


async def _build_view(session: AsyncSession, verdict: Verdict) -> DraftView:
    group = await session.get(ClaimGroup, verdict.claim_group_id)
    if group is None:  # pragma: no cover - foreign key
        raise NotFound("Draft not found", code=ec.FCK_DRAFT_NOT_FOUND)
    claim = await session.get(Claim, group.representative_claim_id)
    investigation = (
        await session.get(Investigation, verdict.investigation_id)
        if verdict.investigation_id is not None
        else None
    )
    dossier = await _dossier_of_group(session, group.id)
    content_id = (
        dossier.content_id
        if dossier is not None and dossier.content_id is not None
        else (investigation.content_id if investigation is not None else None)
    )
    content = await session.get(ContentItem, content_id) if content_id is not None else None

    note = await session.scalar(select(ContextNote).where(ContextNote.verdict_id == verdict.id))
    notes: dict[str, dict[str, Any]] = {}
    if note is not None:
        rows = await session.scalars(
            select(ContextNoteTranslation)
            .where(ContextNoteTranslation.note_id == note.id)
            .order_by(ContextNoteTranslation.id)
        )
        for tr in rows:
            notes[tr.lang] = {"text": tr.text, "fact_sheet": tr.fact_sheet, "origin": tr.origin}

    flags = dict(verdict.flags or {})
    passage_ids = {
        int(c["passage_id"])
        for item in verdict.rationale or []
        for c in item.get("citations", [])
        if c.get("passage_id") is not None
    }
    passages: dict[int, tuple[EvidencePassage, Source]] = {}
    if passage_ids:
        result = await session.execute(
            select(EvidencePassage, Source)
            .join(Source, Source.id == EvidencePassage.source_id)
            .where(EvidencePassage.id.in_(passage_ids))
        )
        passages = {p.id: (p, s) for p, s in result.all()}

    rationale: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    for item in verdict.rationale or []:
        citations: list[dict[str, Any]] = []
        for c in item.get("citations", []):
            found = passages.get(int(c["passage_id"])) if c.get("passage_id") is not None else None
            passage, source = found if found is not None else (None, None)
            src: dict[str, Any] | None = (
                {
                    "url": source.canonical_url,
                    "publisher": source.publisher,
                    "title": source.title,
                    "tier": source.tier,
                }
                if source is not None
                else None
            )
            if source is not None and src is not None:
                sources.setdefault(source.canonical_url, src)
            citations.append(
                {
                    "label": c.get("label") or (passage.label if passage is not None else None),
                    "quote": c.get("quote", ""),
                    "valid": bool(c.get("valid", True)),
                    "lang": passage.lang if passage is not None else None,
                    "translation_fr": passage.translation_fr if passage is not None else None,
                    "source": src,
                }
            )
        rationale.append({"assertion": item.get("assertion", ""), "citations": citations})

    claim_view: dict[str, Any] = {}
    if claim is not None:
        claim_view = {
            "text_original": claim.text_original,
            "lang": claim.lang_original,
            "text_fr": claim.text_fr,
        }
    content_view = (
        {
            "platform": content.platform,
            "external_id": content.external_id,
            "url": content_url(content.platform, content.external_id),
            "lang": content.lang,
        }
        if content is not None
        else None
    )
    return DraftView(
        draft_id=str(verdict.public_id),
        seq=verdict.id,
        version=verdict.version,
        status=verdict.status,
        level=group.level,
        depth=flags.get("depth") or (investigation.depth if investigation is not None else None),
        veracity=verdict.veracity,
        context_need=verdict.context_need,
        context_types=list(verdict.context_types or []),
        confidence=verdict.confidence,
        confidence_band=flags.get("confidence_band"),
        claim=claim_view,
        content=content_view,
        dossier=(
            {"id": str(dossier.public_id), "status": dossier.status}
            if dossier is not None
            else None
        ),
        notes=notes,
        links=list(note.links or []) if note is not None else [],
        rationale=rationale,
        not_in_sources=list(verdict.not_in_sources or []),
        review_points=list(verdict.review_points or []),
        objection=flags.get("objection"),
        critic=flags.get("critic"),
        checks=dict(flags.get("checks") or {}),
        flags={k: v for k, v in flags.items() if k not in _NOTE_FLAG_KEYS},
        sources=list(sources.values()),
        drafted_at=verdict.drafted_at,
        notified_at=verdict.notified_at,
    )


async def list_pending_drafts(
    session: AsyncSession,
    actor: Actor,
    *,
    notified: bool | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> list[DraftView]:
    """Drafts waiting for a decision: status ``draft`` and in the review queue
    (``drafted_at`` set). Newest first; ``before_id`` is the internal verdict id of the
    last row of the previous page."""
    actor.require(Permission.DOSSIER_READ)
    stmt = (
        select(Verdict)
        .where(Verdict.status == VerdictStatus.DRAFT.value, Verdict.drafted_at.is_not(None))
        .order_by(Verdict.id.desc())
        .limit(min(max(limit, 1), 200))
    )
    if notified is True:
        stmt = stmt.where(Verdict.notified_at.is_not(None))
    elif notified is False:
        stmt = stmt.where(Verdict.notified_at.is_(None))
    if before_id is not None:
        stmt = stmt.where(Verdict.id < before_id)
    verdicts = (await session.scalars(stmt)).all()
    return [await _build_view(session, v) for v in verdicts]


async def _verdict_by_public_id(
    session: AsyncSession, public_id: uuid.UUID, *, lock: bool = False
) -> Verdict:
    stmt = select(Verdict).where(Verdict.public_id == public_id)
    if lock:
        stmt = stmt.with_for_update()
    verdict = await session.scalar(stmt)
    if verdict is None or verdict.drafted_at is None:
        raise NotFound("Draft not found", code=ec.FCK_DRAFT_NOT_FOUND, public_id=str(public_id))
    return verdict


async def get_draft(session: AsyncSession, actor: Actor, public_id: uuid.UUID) -> DraftView:
    actor.require(Permission.DOSSIER_READ)
    return await _build_view(session, await _verdict_by_public_id(session, public_id))


async def mark_draft_notified(session: AsyncSession, actor: Actor, public_id: uuid.UUID) -> None:
    """Record that the draft is displayed in Discord. Operational marker: no audit, no
    event, no version bump (plain SQL, the row has an optimistic version column)."""
    actor.require(Permission.DOSSIER_READ)
    result = await session.execute(
        text(
            "UPDATE verdicts SET notified_at = now() "
            "WHERE public_id = :p AND drafted_at IS NOT NULL AND notified_at IS NULL"
        ),
        {"p": public_id},
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        exists = await session.scalar(
            select(Verdict.id).where(
                Verdict.public_id == public_id, Verdict.drafted_at.is_not(None)
            )
        )
        if exists is None:
            raise NotFound("Draft not found", code=ec.FCK_DRAFT_NOT_FOUND, public_id=str(public_id))


# --- review ----------------------------------------------------------------------------


def _bad_review(message: str, **details: object) -> InvalidInput:
    return InvalidInput(message, code=ec.FCK_BAD_REVIEW, **details)


def _clean_labels(labels: Sequence[str]) -> list[str]:
    valid = {label.value for label in ReviewLabel}
    out: list[str] = []
    for label in labels:
        if label not in valid:
            raise _bad_review("Unknown review label", label=str(label)[:64])
        if label not in out:
            out.append(label)
    return out


async def _advance_dossier(
    session: AsyncSession, actor: Actor, dossier: Dossier | None, *targets: DossierStatus
) -> None:
    """Move the dossier through ``targets`` in order, skipping steps already done or no
    longer reachable (a dossier can hold several drafts)."""
    if dossier is None:
        return
    for target in targets:
        current = DossierStatus(dossier.status)
        if current is target:
            continue
        if target not in allowed_transitions(DossierKind(dossier.kind), current):
            continue
        await dossier_uc.transition(
            session, actor, dossier.public_id, target, reason="fact-check draft review"
        )


async def review_draft(
    session: AsyncSession,
    actor: Actor,
    public_id: uuid.UUID,
    *,
    decision: ReviewDecision | str,
    veracity: str | None = None,
    note_text: str | None = None,
    note_lang: str | None = None,
    labels: Sequence[str] = (),
    reason: str | None = None,
    expected_version: int | None = None,
    validate_band: bool = False,
) -> ReviewResult:
    """Decide a draft. A reviewer endorses (the draft waits for an editor); an editor
    validates (verdict active, ``verdict.published``) or rejects. With ``validate_band``,
    a validation also validates the value band of the group (same transaction)."""
    actor.require(Permission.DOSSIER_REVIEW)
    try:
        decision = ReviewDecision(decision)
    except ValueError:
        raise _bad_review("Unknown review decision", decision=str(decision)[:32]) from None
    if decision is ReviewDecision.REJECT:
        actor.require(Permission.DOSSIER_DECIDE)
    clean_labels = _clean_labels(labels)
    reason = reason.strip() if reason else None
    new_veracity: Veracity | None = None
    if veracity:
        try:
            new_veracity = Veracity(veracity)
        except ValueError:
            raise _bad_review("Unknown veracity", veracity=str(veracity)[:32]) from None
    new_note = note_text.strip() if note_text else None
    lang = (note_lang or "fr").strip().lower()
    if decision is ReviewDecision.REJECT and not reason:
        raise _bad_review("A reason is required to reject a draft")
    if decision is ReviewDecision.EDIT:
        if new_veracity is None and not new_note:
            raise _bad_review("An edit needs a veracity or a note text")
        if new_note:
            if lang not in REFERENCE_LANGS:
                raise _bad_review("The note language must be fr or en", note_lang=lang[:_MAX_LANG])
            if note_length(new_note) > NOTE_MAX_CHARS:
                raise _bad_review("The note is too long (links excluded)", max_chars=NOTE_MAX_CHARS)

    verdict = await _verdict_by_public_id(session, public_id, lock=True)
    if verdict.status != VerdictStatus.DRAFT.value:
        raise Conflict(
            "The draft was already decided",
            code=ec.FCK_DRAFT_ALREADY_REVIEWED,
            status=verdict.status,
        )
    if expected_version is not None and verdict.version != expected_version:
        raise Conflict(
            "The draft was modified in the meantime",
            expected=expected_version,
            current=verdict.version,
        )

    group = await session.get(ClaimGroup, verdict.claim_group_id)
    if group is None:  # pragma: no cover - foreign key
        raise NotFound("Draft not found", code=ec.FCK_DRAFT_NOT_FOUND)
    dossier = await _dossier_of_group(session, group.id)
    note = await session.scalar(
        select(ContextNote).where(ContextNote.verdict_id == verdict.id).with_for_update()
    )
    translations: dict[str, ContextNoteTranslation] = {}
    if note is not None:
        translations = {
            tr.lang: tr
            for tr in await session.scalars(
                select(ContextNoteTranslation).where(ContextNoteTranslation.note_id == note.id)
            )
        }
    seconds = await session.scalar(
        select(
            func.extract(
                "epoch", func.now() - func.coalesce(verdict.notified_at, verdict.drafted_at)
            )
        )
    )
    review_seconds = max(0, int(seconds or 0))
    reviewer = actor.label

    old_veracity = verdict.veracity
    diff: dict[str, Any] = {}
    if decision is ReviewDecision.EDIT:
        if new_veracity is not None and new_veracity.value != old_veracity:
            diff["veracity"] = [old_veracity, new_veracity.value]
        if new_note:
            before = translations[lang].text if lang in translations else None
            if new_note != before:
                diff["note"] = {"lang": lang, "before": before, "after": new_note}

    can_decide = actor.can(Permission.DOSSIER_DECIDE)
    retranslate_from: str | None = None
    if decision is ReviewDecision.REJECT:
        outcome = ReviewOutcome.REJECTED
    elif not can_decide:
        outcome = ReviewOutcome.ENDORSED
    else:
        outcome = ReviewOutcome.VALIDATED

    if outcome is ReviewOutcome.ENDORSED:
        await _advance_dossier(session, actor, dossier, DossierStatus.IN_REVIEW)
    elif outcome is ReviewOutcome.REJECTED:
        await _advance_dossier(
            session, actor, dossier, DossierStatus.IN_REVIEW, DossierStatus.REJECTED
        )
        verdict.status = VerdictStatus.REJECTED.value
        if note is not None:
            note.status = NoteStatus.REJECTED.value
            note.updated_at = func.now()
    else:
        await _advance_dossier(
            session, actor, dossier, DossierStatus.IN_REVIEW, DossierStatus.VALIDATED
        )
        previous = (
            await session.scalars(
                select(Verdict)
                .where(
                    Verdict.claim_group_id == group.id,
                    Verdict.status == VerdictStatus.ACTIVE.value,
                    Verdict.id != verdict.id,
                )
                .with_for_update()
            )
        ).all()
        for old in previous:
            old.status = VerdictStatus.SUPERSEDED.value
        await session.flush()  # free the unique "one active verdict per group" slot
        if new_veracity is not None:
            verdict.veracity = new_veracity.value
        verdict.status = VerdictStatus.ACTIVE.value
        verdict.validated_at = func.now()
        verdict.expires_at = await _expiry_of(session, group, note)
        verdict.validated_by = reviewer
        verdict.updated_at = func.now()
        group.active_verdict_id = verdict.id
        group.state = ClaimGroupState.VERIFIED.value
        group.updated_at = func.now()
        if note is None and decision is ReviewDecision.EDIT and new_note:
            # The draft had no note (no evidence, refusal): the team writes the first one.
            note = ContextNote(
                verdict_id=verdict.id,
                claim_group_id=group.id,
                regime=Regime.FORMULATION.value,
                text=new_note,
                lang=lang,
                links=[],
                status=NoteStatus.VALIDATED.value,
            )
            session.add(note)
            await session.flush()
        if note is not None:
            note.status = NoteStatus.VALIDATED.value
            note.updated_at = func.now()
            if decision is ReviewDecision.EDIT and new_note:
                row = translations.get(lang)
                if row is None:
                    row = ContextNoteTranslation(note_id=note.id, lang=lang, text=new_note)
                    session.add(row)
                    translations[lang] = row
                row.text = new_note
                row.origin = TranslationOrigin.HUMAN.value
                row.validated_by = reviewer
                row.updated_at = func.now()
                retranslate_from = lang
            for tr_lang, tr in translations.items():
                if tr_lang in REFERENCE_LANGS and tr_lang != retranslate_from:
                    tr.origin = TranslationOrigin.ORIGINAL.value
                    tr.validated_by = reviewer
                    tr.updated_at = func.now()
    await session.flush()
    await session.refresh(verdict)

    session.add(
        Review(
            verdict_id=verdict.id,
            dossier_id=dossier.id if dossier is not None else None,
            reviewer=reviewer,
            decision=decision.value,
            outcome=outcome.value,
            final_veracity=verdict.veracity,
            diff=diff,
            labels=clean_labels,
            reason=reason,
            review_seconds=review_seconds,
        )
    )
    dossier_public = str(dossier.public_id) if dossier is not None else None
    await publish(
        session,
        names.DRAFT_REVIEWED,
        {
            "draft_id": str(verdict.public_id),
            "dossier_id": dossier_public,
            "decision": decision.value,
            "outcome": outcome.value,
            "final_veracity": verdict.veracity,
            "labels": clean_labels,
            "reviewer": reviewer,
        },
        aggregate_type=AGGREGATE,
        aggregate_id=str(verdict.public_id),
        actor=reviewer,
    )
    if outcome is ReviewOutcome.VALIDATED:
        content = await _content_of(session, dossier, verdict)
        await publish(
            session,
            names.VERDICT_PUBLISHED,
            {
                "verdict_id": str(verdict.public_id),
                "group_id": str(group.public_id),
                "dossier_id": dossier_public,
                "veracity": verdict.veracity,
                "context_need": verdict.context_need,
                "platform": content.platform if content is not None else None,
                "external_id": content.external_id if content is not None else None,
                "retranslate_from": retranslate_from,
            },
            aggregate_type=AGGREGATE,
            aggregate_id=str(verdict.public_id),
            actor=reviewer,
        )
        if validate_band:
            await references_uc.validate_band_from_verdict(
                session, actor, verdict_id=verdict.public_id
            )
    return ReviewResult(outcome, await _build_view(session, verdict))


async def _expiry_of(
    session: AsyncSession, group: ClaimGroup, note: ContextNote | None
) -> datetime:
    """Expiry of the verdict being validated: claim-type TTL, next publication of the
    group's validated reference fact, 6 h when its note is provisional."""
    settings = get_settings()
    claim = await session.get(Claim, group.representative_claim_id)
    claim_type = ClaimType(claim.claim_type) if claim is not None else ClaimType.OTHER
    try:
        overrides = parse_ttl_overrides(settings.fc_verdict_ttl_days)
    except ValueError:
        log.warning("factcheck.bad_ttl_overrides")
        overrides = {}
    next_publication = None
    if group.reference_fact_id is not None:
        fact = await session.get(ReferenceFact, group.reference_fact_id)
        if fact is not None and fact.status == FactStatus.VALIDATED.value:
            next_publication = fact.next_publication
    return verdict_expiry(
        validated_at=datetime.now(UTC),
        claim_type=claim_type,
        overrides=overrides,
        next_publication=next_publication,
        has_provisional_note=note is not None and note.regime == Regime.PROVISIONAL.value,
    )


async def _content_of(
    session: AsyncSession, dossier: Dossier | None, verdict: Verdict
) -> ContentItem | None:
    content_id = dossier.content_id if dossier is not None else None
    if content_id is None and verdict.investigation_id is not None:
        content_id = await session.scalar(
            select(Investigation.content_id).where(Investigation.id == verdict.investigation_id)
        )
    return await session.get(ContentItem, content_id) if content_id is not None else None


# --- manual request --------------------------------------------------------------------


async def request_factcheck(
    session: AsyncSession,
    actor: Actor,
    *,
    platform: str | None = None,
    external_id: str | None = None,
    url: str | None = None,
) -> RequestResult:
    """Queue a manual fact-check of one content (the job reads it and investigates)."""
    actor.require(Permission.FACTCHECK_REQUEST)
    if url:
        plat, ext = parse_content_url(url)
    elif platform and external_id:
        plat = parse_platform(platform).value
        ext = validate_external_id(Platform(plat), external_id)
    else:
        raise InvalidInput("Give a content URL, or a platform and an external_id")
    job_id = await enqueue(
        session,
        names.FC_FAST,
        names.JOB_REQUEST,
        {"platform": plat, "external_id": ext, "requested_by": actor.label},
        dedupe_key=names.request_key(plat, ext),
    )
    return RequestResult(plat, ext, job_id, duplicate=job_id is None)


# --- public verdict --------------------------------------------------------------------


def _norm_lang(lang: str | None) -> str | None:
    if not lang:
        return None
    primary = lang.strip().replace("_", "-").split("-", 1)[0].lower()
    return primary[:_MAX_LANG] or None


async def get_public_verdict(
    session: AsyncSession,
    actor: Actor,
    platform: str,
    external_id: str,
    lang: str | None,
) -> PublicVerdict:
    """Validated verdicts of a content (never drafts), notes in the best language:
    requested, then the post language, then English, then French."""
    actor.require(Permission.DOSSIER_READ_PUBLIC)
    not_found = NotFound(
        "No validated verdict for this content",
        code=ec.FCK_VERDICT_NOT_FOUND,
        platform=platform[:32],
    )
    content = await session.scalar(
        select(ContentItem).where(
            ContentItem.platform == platform, ContentItem.external_id == external_id
        )
    )
    if content is None:
        raise not_found
    wanted = _norm_lang(lang)
    rows = await session.execute(
        select(Claim, Verdict)
        .join(ContentClaim, ContentClaim.claim_id == Claim.id)
        .join(
            ClaimGroupMember,
            (ClaimGroupMember.claim_id == Claim.id) & ClaimGroupMember.removed_at.is_(None),
        )
        .join(ClaimGroup, ClaimGroup.id == ClaimGroupMember.group_id)
        .join(Verdict, Verdict.id == ClaimGroup.active_verdict_id)
        .where(ContentClaim.content_id == content.id, Verdict.status == VerdictStatus.ACTIVE.value)
        .order_by(ContentClaim.position, Claim.id)
    )
    claims: list[PublicClaim] = []
    for claim, verdict in rows.all():
        claims.append(
            PublicClaim(
                text=claim.text_fr if wanted == "fr" else claim.text_original,
                lang="fr" if wanted == "fr" else claim.lang_original,
                veracity=verdict.veracity,
                context_need=verdict.context_need,
                context_types=list(verdict.context_types or []),
                validated_at=verdict.validated_at,
                note=await _public_note(session, verdict, wanted, content.lang),
            )
        )
    if not claims:
        raise not_found
    return PublicVerdict(
        platform=content.platform,
        external_id=content.external_id,
        url=content_url(content.platform, content.external_id),
        lang=content.lang,
        claims=claims,
    )


async def _public_note(
    session: AsyncSession, verdict: Verdict, wanted: str | None, post_lang: str
) -> PublicNote | None:
    note = await session.scalar(
        select(ContextNote).where(
            ContextNote.verdict_id == verdict.id,
            ContextNote.status == NoteStatus.VALIDATED.value,
        )
    )
    if note is None:
        return None
    by_lang = {
        tr.lang: tr
        for tr in await session.scalars(
            select(ContextNoteTranslation).where(ContextNoteTranslation.note_id == note.id)
        )
    }
    for candidate in (wanted, post_lang, "en", "fr"):
        if candidate and candidate in by_lang:
            tr = by_lang[candidate]
            return PublicNote(
                lang=tr.lang,
                text=tr.text,
                fact_sheet=tr.fact_sheet,
                machine_translated=tr.origin == TranslationOrigin.MACHINE.value,
                links=list(note.links or []),
            )
    return None
