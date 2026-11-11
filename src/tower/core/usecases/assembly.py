"""Assembly use cases (E10): open the review dossier of a draft, store machine translations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.dossiers import DossierKind
from tower.core.domain.errors import Conflict
from tower.core.domain.factcheck.urls import content_url
from tower.core.domain.factcheck.values import TranslationOrigin
from tower.core.domain.platforms import Platform
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.models.dossiers import Dossier
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ContextNoteTranslation,
    DossierClaimGroup,
    Investigation,
    Verdict,
)
from tower.core.models.ingestion import ContentItem
from tower.core.usecases import dossiers as dossier_uc

__all__ = ["N4_PRIORITY", "open_draft", "store_machine_translations"]

N4_PRIORITY = 50
_TITLE_MAX = 200


async def open_draft(
    session: AsyncSession, actor: Actor, verdict: Verdict, investigation: Investigation
) -> Dossier:
    """Open the ``fact_check`` dossier of a draft verdict, link it to the claim group and
    the content, mark the verdict as drafted (it now counts in the daily quota) and publish
    ``draft.ready``. The caller holds the quota lock when the quota matters."""
    actor.require(Permission.DOSSIER_CREATE)
    if verdict.drafted_at is not None:
        raise Conflict("The draft already has a dossier", draft_id=str(verdict.public_id))
    group = await session.get(ClaimGroup, verdict.claim_group_id)
    if group is None:  # pragma: no cover (foreign key)
        raise Conflict("Claim group missing", group_id=verdict.claim_group_id)
    claim = await session.get(Claim, group.representative_claim_id)
    if claim is None:  # pragma: no cover (foreign key)
        raise Conflict("Representative claim missing", group_id=group.id)
    content = (
        await session.get(ContentItem, investigation.content_id)
        if investigation.content_id is not None
        else None
    )
    x_post_id = (
        int(content.external_id)
        if content is not None and content.platform == Platform.X.value
        else None
    )
    dossier = await dossier_uc.open_dossier(
        session,
        actor,
        kind=DossierKind.FACT_CHECK,
        x_post_id=x_post_id,
        title=claim.text_fr[:_TITLE_MAX],
        priority=N4_PRIORITY if group.level == "N4" else 0,
    )
    if content is not None:
        dossier.content_id = content.id
    session.add(DossierClaimGroup(dossier_id=dossier.id, claim_group_id=group.id))
    verdict.drafted_at = datetime.now(UTC)
    verdict.updated_at = func.now()
    await session.flush()
    await publish(
        session,
        names.DRAFT_READY,
        {
            "draft_id": str(verdict.public_id),
            "dossier_id": str(dossier.public_id),
            "group_id": str(group.public_id),
            "level": group.level,
            "veracity": verdict.veracity,
            "context_need": verdict.context_need,
            "confidence": verdict.confidence,
            "platform": content.platform if content is not None else None,
            "external_id": content.external_id if content is not None else None,
            "url": (
                content_url(content.platform, content.external_id) if content is not None else None
            ),
        },
        aggregate_type="verdict",
        aggregate_id=str(verdict.public_id),
        actor=actor.label,
    )
    return dossier


async def store_machine_translations(
    session: AsyncSession,
    actor: Actor,
    *,
    note_id: int,
    items: Sequence[tuple[str, str, str | None]],
) -> int:
    """Upsert machine translations ``(lang, text, fact_sheet)`` of a note. A ``human`` or
    ``original`` row is never overwritten. Returns the number of rows written."""
    actor.require(Permission.FACTCHECK_REQUEST)
    written = 0
    for lang, text, fact_sheet in items:
        stmt = pg_insert(ContextNoteTranslation).values(
            note_id=note_id,
            lang=lang,
            text=text,
            fact_sheet=fact_sheet,
            origin=TranslationOrigin.MACHINE.value,
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[ContextNoteTranslation.note_id, ContextNoteTranslation.lang],
            set_={
                "text": stmt.excluded.text,
                "fact_sheet": stmt.excluded.fact_sheet,
                "updated_at": func.now(),
            },
            where=ContextNoteTranslation.origin == TranslationOrigin.MACHINE.value,
        ).returning(ContextNoteTranslation.id)
        if (await session.execute(upsert)).first() is not None:
            written += 1
    return written
