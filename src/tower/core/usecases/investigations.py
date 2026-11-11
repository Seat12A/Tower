"""Fact-check investigation outcomes: completion (draft verdict, citations, context note,
retention of evidence) and failure.

Every write checks the permission, applies the domain rules and publishes its event in
the same transaction (see docs/factcheck/pipeline.md E9). The investigation row is locked
for the duration of the transaction: a replayed job completes nothing twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import NotFound
from tower.core.domain.factcheck.values import (
    CONTEXT_REGIME,
    PROVISIONAL_NOTE_TTL_S,
    ContextType,
    InvestigationStage,
    NoteStatus,
    Regime,
    TranslationOrigin,
    VerdictStatus,
)
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.models.factcheck import (
    Citation,
    ClaimGroup,
    ContextNote,
    ContextNoteTranslation,
    Investigation,
    Verdict,
)

__all__ = [
    "CitationFields",
    "NoteFields",
    "NoteTranslationFields",
    "VerdictFields",
    "complete_investigation",
    "fail_investigation",
]

AGGREGATE = "investigation"
_FINAL_STAGES = frozenset(
    {
        InvestigationStage.COMPLETED.value,
        InvestigationStage.ASSEMBLED.value,
        InvestigationStage.FAILED.value,
    }
)
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True, slots=True)
class VerdictFields:
    veracity: str
    context_need: str
    context_types: Sequence[str]
    rationale: Sequence[Mapping[str, Any]]
    not_in_sources: Sequence[str]
    review_points: Sequence[str]
    flags: Mapping[str, Any]
    prompt_versions: Mapping[str, Any]
    route_versions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CitationFields:
    passage_id: int
    quote: str
    assertion: str
    valid: bool
    position: int


@dataclass(frozen=True, slots=True)
class NoteTranslationFields:
    lang: str
    text: str
    fact_sheet: str | None


@dataclass(frozen=True, slots=True)
class NoteFields:
    """Writer output: ``text``/``lang`` = the post-language version."""

    text: str
    lang: str
    links: Sequence[str]
    primary_context_type: ContextType | None
    checks: Mapping[str, Any]
    critic: Mapping[str, Any] | None
    translations: Sequence[NoteTranslationFields] = field(default_factory=tuple)


_RETAIN = text(
    """
    UPDATE evidence_passages SET
        kept = (id = ANY(CAST(:cited AS bigint[]))),
        text = CASE WHEN id = ANY(CAST(:cited AS bigint[])) THEN text ELSE NULL END
    WHERE investigation_id = :investigation
    """
)
_SET_ORIGIN = text("UPDATE evidence_passages SET origin = :origin WHERE id = :id")
_SET_TRANSLATION = text("UPDATE evidence_passages SET translation_fr = :text WHERE id = :id")


async def _lock(session: AsyncSession, investigation: Investigation) -> Investigation:
    locked = await session.scalar(
        select(Investigation)
        .where(Investigation.id == investigation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise NotFound(
            "Investigation not found",
            code=ec.FCK_DRAFT_NOT_FOUND,
            investigation_id=str(investigation.id),
        )
    return locked


async def _event(
    session: AsyncSession,
    actor: Actor,
    inv: Investigation,
    *,
    outcome: str,
    verdict: Verdict | None,
) -> None:
    group = await session.get(ClaimGroup, inv.claim_group_id)
    await publish(
        session,
        names.INVESTIGATION_COMPLETED,
        {
            "investigation_id": str(inv.public_id),
            "group_id": str(group.public_id) if group is not None else None,
            "level": inv.level,
            "depth": inv.depth,
            "verdict_id": str(verdict.public_id) if verdict is not None else None,
            "veracity": verdict.veracity if verdict is not None else None,
            "context_need": verdict.context_need if verdict is not None else None,
            "outcome": outcome,
        },
        aggregate_type=AGGREGATE,
        aggregate_id=str(inv.public_id),
        actor=actor.label,
    )


async def complete_investigation(
    session: AsyncSession,
    actor: Actor,
    investigation: Investigation,
    *,
    outcome: str,
    verdict: VerdictFields | None,
    citations: Sequence[CitationFields] = (),
    note: NoteFields | None = None,
    passage_translations: Mapping[int, str] | None = None,
    passage_origins: Mapping[int, str] | None = None,
    cost_usd_micros: int = 0,
    updates: Mapping[str, Any] | None = None,
    flags: Mapping[str, Any] | None = None,
) -> Verdict | None:
    """Store the final outputs of an investigation, in one transaction.

    ``outcome``: ``drafted`` or ``unverifiable_at_framing``. Writes the draft verdict (status
    ``draft``, not yet in the review queue), its citation rows, the context note with one
    ``machine`` translation per writer language, the origins and translations of passages,
    then keeps the text of cited passages only (sources and links are always kept).
    ``updates``: extra investigation columns (``framing``…); ``flags``: replaces the
    investigation flags. Returns ``None`` when the investigation already reached a final
    stage (replay)."""
    actor.require(Permission.FACTCHECK_REQUEST)
    inv = await _lock(session, investigation)
    if inv.stage in _FINAL_STAGES:
        return None
    now = datetime.now(UTC)
    for key, value in (updates or {}).items():
        setattr(inv, key, value)
    if flags is not None:
        inv.flags = dict(flags)
    inv.cost_usd_micros = inv.cost_usd_micros + cost_usd_micros
    inv.stage = InvestigationStage.COMPLETED.value
    inv.completed_at = now
    inv.updated_at = now

    created: Verdict | None = None
    if verdict is not None:
        created = Verdict(
            claim_group_id=inv.claim_group_id,
            investigation_id=inv.id,
            veracity=verdict.veracity,
            context_need=verdict.context_need,
            context_types=list(verdict.context_types),
            status=VerdictStatus.DRAFT.value,
            rationale=[dict(r) for r in verdict.rationale],
            not_in_sources=list(verdict.not_in_sources),
            review_points=list(verdict.review_points),
            flags=dict(verdict.flags),
            prompt_versions=dict(verdict.prompt_versions),
            route_versions=dict(verdict.route_versions),
            expires_at=None,
        )
        session.add(created)
        await session.flush()
        await session.refresh(created)
        for c in citations:
            session.add(
                Citation(
                    verdict_id=created.id,
                    passage_id=c.passage_id,
                    quote=c.quote,
                    assertion=c.assertion,
                    valid=c.valid,
                    position=c.position,
                )
            )
        if note is not None:
            await _add_note(session, inv, created, note, now)

    for passage_id, origin in (passage_origins or {}).items():
        await session.execute(_SET_ORIGIN, {"id": passage_id, "origin": origin})
    for passage_id, translated in (passage_translations or {}).items():
        await session.execute(_SET_TRANSLATION, {"id": passage_id, "text": translated})
    cited = sorted({c.passage_id for c in citations})
    await session.execute(_RETAIN, {"investigation": inv.id, "cited": cited})
    await _event(session, actor, inv, outcome=outcome, verdict=created)
    return created


async def _add_note(
    session: AsyncSession,
    inv: Investigation,
    verdict: Verdict,
    note: NoteFields,
    now: datetime,
) -> None:
    regime = (
        CONTEXT_REGIME[note.primary_context_type]
        if note.primary_context_type is not None
        else Regime.FORMULATION
    )
    row = ContextNote(
        verdict_id=verdict.id,
        claim_group_id=inv.claim_group_id,
        context_type=note.primary_context_type.value if note.primary_context_type else None,
        regime=regime.value,
        text=note.text,
        lang=note.lang,
        links=list(note.links),
        status=NoteStatus.DRAFT.value,
        expires_at=(
            now + timedelta(seconds=PROVISIONAL_NOTE_TTL_S)
            if regime is Regime.PROVISIONAL
            else None
        ),
        checks=dict(note.checks),
        critic=dict(note.critic) if note.critic is not None else None,
    )
    session.add(row)
    await session.flush()
    for t in note.translations:
        session.add(
            ContextNoteTranslation(
                note_id=row.id,
                lang=t.lang,
                text=t.text,
                fact_sheet=t.fact_sheet,
                origin=TranslationOrigin.MACHINE.value,
            )
        )


async def fail_investigation(
    session: AsyncSession,
    actor: Actor,
    investigation: Investigation,
    *,
    error: str,
    cost_usd_micros: int = 0,
) -> bool:
    """Mark the investigation ``failed`` (short error text) and publish
    ``investigation.completed`` with outcome ``failed``. False when already final."""
    actor.require(Permission.FACTCHECK_REQUEST)
    inv = await _lock(session, investigation)
    if inv.stage in _FINAL_STAGES:
        return False
    now = datetime.now(UTC)
    inv.stage = InvestigationStage.FAILED.value
    inv.error = error[:_MAX_ERROR_CHARS]
    inv.cost_usd_micros = inv.cost_usd_micros + cost_usd_micros
    inv.completed_at = now
    inv.updated_at = now
    await _event(session, actor, inv, outcome="failed", verdict=None)
    return True
