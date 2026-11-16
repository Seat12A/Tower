"""Manual notes: the team writes a claim, a verdict and a context note without any model.

The result is an ordinary draft (claim, group, verdict ``draft``, note, case,
``draft.ready``) reviewed like the others: an editor validates it, which publishes it.
When the post is not in the database yet, a ``factcheck.manual`` job reads it from the
platform first (no model call). See docs/fact-check.md (Notes manuelles).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.errors import InvalidInput
from tower.core.domain.factcheck.manual import MANUAL_PROMPT_VERSION, ManualNote
from tower.core.domain.factcheck.urls import parse_content_url
from tower.core.domain.factcheck.values import (
    ClaimGroupState,
    ExtractionStatus,
    GroupEventKind,
    NoteStatus,
    Regime,
    TranslationOrigin,
    VerdictStatus,
)
from tower.core.domain.platforms import Platform, parse_platform, validate_external_id
from tower.core.domain.security import Actor, Permission
from tower.core.factcheck import names
from tower.core.jobs.queue import enqueue
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    ContextNote,
    ContextNoteTranslation,
    Verdict,
)
from tower.core.models.ingestion import ContentItem
from tower.core.usecases import assembly
from tower.core.usecases import claims as claims_uc

__all__ = ["MANUAL_LEVEL", "ManualResult", "create_manual_draft", "submit_manual_note"]

MANUAL_LEVEL = "N4"
"""Manual notes are team requests: highest level, never reused without review."""


@dataclass(frozen=True, slots=True)
class ManualResult:
    platform: str
    external_id: str
    status: str
    """``drafted`` (draft opened now) or ``queued`` (the post is read first)."""
    draft_id: str | None
    duplicate: bool


def resolve_post(url: str | None, platform: str | None, external_id: str | None) -> tuple[str, str]:
    if url:
        return parse_content_url(url)
    if platform and external_id:
        plat = parse_platform(platform).value
        return plat, validate_external_id(Platform(plat), external_id)
    raise InvalidInput("Give a content URL, or a platform and an external_id")


async def _existing(session: AsyncSession, content_id: int, key: str) -> Verdict | None:
    """Manual draft with this key among the verdicts of the content's claims."""
    return await session.scalar(
        select(Verdict)
        .join(ClaimGroupMember, ClaimGroupMember.group_id == Verdict.claim_group_id)
        .join(ContentClaim, ContentClaim.claim_id == ClaimGroupMember.claim_id)
        .where(ContentClaim.content_id == content_id, Verdict.flags["manual_key"].astext == key)
        .limit(1)
    )


async def submit_manual_note(
    session: AsyncSession,
    actor: Actor,
    note: ManualNote,
    *,
    url: str | None = None,
    platform: str | None = None,
    external_id: str | None = None,
) -> ManualResult:
    """Open the draft now when the post is known, else queue ``factcheck.manual``."""
    actor.require(Permission.CASE_REVIEW)
    actor.require(Permission.CASE_CREATE)
    plat, ext = resolve_post(url, platform, external_id)
    key = note.key(plat, ext, actor.label)
    content = await session.scalar(
        select(ContentItem).where(ContentItem.platform == plat, ContentItem.external_id == ext)
    )
    if content is not None:
        existing = await _existing(session, content.id, key)
        if existing is not None:
            return ManualResult(plat, ext, "drafted", str(existing.public_id), duplicate=True)
        verdict = await create_manual_draft(
            session, actor, content=content, note=note, author=actor.label, key=key
        )
        return ManualResult(plat, ext, "drafted", str(verdict.public_id), duplicate=False)
    job_id = await enqueue(
        session,
        names.FC_FAST,
        names.JOB_MANUAL,
        {
            "platform": plat,
            "external_id": ext,
            "author": actor.label,
            "key": key,
            "note": note.to_payload(),
        },
        priority=names.FAST_LANE_PRIORITY,
        dedupe_key=names.manual_key(key),
    )
    return ManualResult(plat, ext, "queued", None, duplicate=job_id is None)


async def create_manual_draft(
    session: AsyncSession,
    actor: Actor,
    *,
    content: ContentItem,
    note: ManualNote,
    author: str,
    key: str,
) -> Verdict:
    """Claim, group (N4), draft verdict, note and case of a manual note; publishes
    ``case.opened`` and ``draft.ready``. Idempotent on ``key`` (returns the first draft).

    The content's ``content_extractions`` row is set to ``extracted``: the automatic
    extraction skips a content that already has claims, so no model runs on it later."""
    actor.require(Permission.CASE_CREATE)
    existing = await _existing(session, content.id, key)
    if existing is not None:
        return existing
    position = await session.scalar(
        select(func.coalesce(func.max(ContentClaim.position) + 1, 0)).where(
            ContentClaim.content_id == content.id
        )
    )
    claim = Claim(
        public_id=uuid.uuid4(),
        lang_original="fr",
        text_original=note.claim_text,
        text_fr=note.claim_text,
        claim_type=note.claim_type.value,
        verifiability=1.0,
        prompt_version=MANUAL_PROMPT_VERSION,
    )
    session.add(claim)
    await session.flush()
    session.add(ContentClaim(content_id=content.id, claim_id=claim.id, position=position or 0))
    reach = (content.metrics or {}).get("impressions")
    group = ClaimGroup(
        public_id=uuid.uuid4(),
        representative_claim_id=claim.id,
        level=MANUAL_LEVEL,
        state=ClaimGroupState.INVESTIGATING.value,
        reach=int(reach) if isinstance(reach, int | float) and reach > 0 else 0,
    )
    session.add(group)
    await session.flush()
    session.add(ClaimGroupMember(group_id=group.id, claim_id=claim.id))
    await claims_uc.journal_event(
        session,
        actor,
        group,
        GroupEventKind.CREATED,
        claim_ids=[claim.id],
        to_level=MANUAL_LEVEL,
        data={"rule": "manual", "author": author},
    )
    verdict = Verdict(
        public_id=uuid.uuid4(),
        claim_group_id=group.id,
        veracity=note.veracity.value,
        context_need=note.context_need.value,
        context_types=[t.value for t in note.context_types],
        status=VerdictStatus.DRAFT.value,
        flags={"manual": True, "author": author, "manual_key": key},
        prompt_versions={"manual": MANUAL_PROMPT_VERSION},
    )
    session.add(verdict)
    await session.flush()
    if note.note_text is not None:
        context_note = ContextNote(
            verdict_id=verdict.id,
            claim_group_id=group.id,
            context_type=note.context_types[0].value if note.context_types else None,
            regime=Regime.FORMULATION.value,
            text=note.note_text,
            lang=note.note_lang,
            links=list(note.links),
            status=NoteStatus.DRAFT.value,
        )
        session.add(context_note)
        await session.flush()
        session.add(
            ContextNoteTranslation(
                note_id=context_note.id,
                lang=note.note_lang,
                text=note.note_text,
                fact_sheet=note.fact_sheet,
                origin=TranslationOrigin.HUMAN.value,
            )
        )
    await claims_uc.mark_extraction(
        session, content.id, MANUAL_LEVEL, ExtractionStatus.EXTRACTED, None, None
    )
    await assembly.open_draft_for_content(session, actor, verdict, content)
    await session.flush()
    await session.refresh(verdict)
    return verdict
