"""Jobs and subscription handlers of the ``factcheck`` service (E2 extraction, manual
requests, E10 assembly, translations). See docs/factcheck/pipeline.md."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.budget import BudgetRefused
from tower.core.config import get_settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.fidelity import (
    check_claim_fidelity,
    check_omission,
    extract_numbers,
)
from tower.core.domain.factcheck.values import AbsenceReason, TranslationOrigin
from tower.core.domain.ingestion import AuthorInput, ItemInput, MediaInput
from tower.core.domain.llm import Level
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.events.outbox import EventRecord
from tower.core.factcheck import inputs, names, runtime
from tower.core.factcheck.levels import override_level
from tower.core.factcheck.profiles import active_profile
from tower.core.factcheck.schemas import ExtractedClaim, ExtractOutput, Signature, TranslateOutput
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.llm.errors import (
    AllRefused,
    BudgetExhausted,
    LLMRouterError,
    NoValidOutput,
    to_job_error,
)
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    ContextNote,
    ContextNoteTranslation,
    Investigation,
    Verdict,
)
from tower.core.models.ingestion import ContentItem, WatchedAccount
from tower.core.ports.llm import LLMRequest, LLMResult
from tower.core.ports.platforms import (
    ContentItem as PlatformItem,
)
from tower.core.ports.platforms import (
    PlatformError,
    PlatformNotConfigured,
    PlatformRateLimited,
    PlatformUnavailable,
    UnsupportedOperation,
)
from tower.core.services.base import JobContext
from tower.core.usecases import assembly as assembly_uc
from tower.core.usecases import claims as claims_uc
from tower.core.usecases.ingestion import ingest_items
from tower.services.factcheck import assembly as asm
from tower.services.factcheck import matching

log = get_logger(__name__)

QUEUE = names.FC_FAST
REPLAY_NOTE = (
    "The previous extraction added a figure or entity absent from the post, or dropped "
    "the post's figures. Extract again, strictly from the post."
)
_MIN_RETRY_S = 60.0
_MAX_RETRY_S = 6 * 3600.0
_EXTRACT_MAX_TOKENS = 3000
_NOTE_MAX_TOKENS = 1000
_SHEET_MAX_TOKENS = 2500
_SCALES = {
    "thousand": Decimal(1000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
}
_REFERENCE_LANGS = ("fr", "en")


def _actor() -> Actor:
    return Actor.system(names.FACTCHECK_ACTOR)


# --------------------------------------------------------------------- model calls


def _retry_delay(retry_at: datetime) -> float:
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    delay = (retry_at - datetime.now(UTC)).total_seconds()
    return min(max(delay, _MIN_RETRY_S), _MAX_RETRY_S)


async def _complete(ctx: JobContext, request: LLMRequest) -> LLMResult:
    """One router call with the job error policy. ``AllRefused`` is left to the caller."""
    llm = runtime.get_llm(ctx.settings, ctx.db)
    try:
        return await llm.complete(request)
    except BudgetExhausted as exc:
        raise RetryLater(_retry_delay(exc.retry_at), "model budget exhausted") from exc
    except AllRefused:
        raise
    except NoValidOutput as exc:
        if ctx.job.attempts >= ctx.job.max_attempts:
            capture(ec.FCK_STAGE_FAILED, exc, stage=ctx.job.kind, role=request.role)
            raise PermanentError("no valid model output") from exc
        raise
    except LLMRouterError as exc:
        raise to_job_error(exc) from exc


# ------------------------------------------------------------------ E2: extraction


def _fmt(value: float) -> str:
    return format(Decimal(repr(value)).normalize(), "f")


def _covered(value: float, available: frozenset[str], scale: str | None) -> bool:
    """A signature bound is derived from a number of the post: the stated value itself, the
    bounds of its precision interval (``7 %`` gives 6.5 to 7.5), or the same scaled by the
    unit scale (``2 million``)."""
    v = Decimal(repr(value))
    factors = [Decimal(1)]
    if scale in _SCALES:
        factors.append(_SCALES[str(scale)])
    for number in available:
        base = Decimal(number)
        half = Decimal("0.5") * Decimal(10) ** -(len(number.split(".")[1]) if "." in number else 0)
        if any(abs(v - base * f) <= half * f for f in factors):
            return True
    return False


def _signature_values(sig: Signature) -> list[float]:
    return [v for v in (sig.value_low, sig.value_high) if v is not None]


def _numbers_text(claim: ExtractedClaim, available: frozenset[str]) -> str:
    """Claim text plus the signature values that no post number explains (checked as
    claim numbers by the contamination rule)."""
    extra = [
        _fmt(v)
        for v in _signature_values(claim.signature)
        if not _covered(v, available, claim.signature.unit_scale)
    ]
    return " ".join([claim.text_original, *extra])


def _full_text(claim: ExtractedClaim) -> str:
    return " ".join([claim.text_original, *(_fmt(v) for v in _signature_values(claim.signature))])


def _faithful(
    output: ExtractOutput, post_text: str, context_text: str, content_id: int
) -> tuple[list[ExtractedClaim], bool]:
    """Claims passing the contamination check, and whether any fidelity failure occurred
    (contaminated claim or omission)."""
    available = extract_numbers(post_text) | extract_numbers(context_text)
    kept: list[ExtractedClaim] = []
    failed = False
    for claim in output.claims:
        issues = check_claim_fidelity(
            _numbers_text(claim, available),
            [e.name for e in claim.entities],
            post_text,
            context_text=context_text,
        )
        if issues:
            failed = True
            capture(
                ec.FCK_EXTRACTION_UNFAITHFUL,
                content=content_id,
                kinds=sorted({i.kind for i in issues}),
            )
            continue
        kept.append(claim)
    has_absence = output.absence_reason not in (None, AbsenceReason.NO_CLAIM)
    if check_omission(post_text, [_full_text(c) for c in output.claims], has_absence=has_absence):
        failed = True
        capture(ec.FCK_EXTRACTION_UNFAITHFUL, content=content_id, kinds=["omission"])
    return kept, failed


async def _thread_text(session: AsyncSession, content: ContentItem) -> tuple[str | None, str]:
    """Kind and text of the parent (reply) or quoted post when already in the database."""
    for kind, ref in (("reply", content.parent_external_id), ("quote", content.quoted_external_id)):
        if ref is None:
            continue
        found = await session.scalar(
            select(ContentItem.text).where(
                ContentItem.platform == content.platform, ContentItem.external_id == ref
            )
        )
        if found:
            return kind, found
    return None, ""


async def _promote_groups(
    session: AsyncSession,
    actor: Actor,
    content: ContentItem,
    claim_ids: Sequence[int],
    requested_by: str,
    language_not_processed: bool,
) -> None:
    groups = (
        await session.scalars(
            select(ClaimGroup)
            .join(ClaimGroupMember, ClaimGroupMember.group_id == ClaimGroup.id)
            .where(ClaimGroupMember.claim_id.in_(claim_ids), ClaimGroupMember.removed_at.is_(None))
            .order_by(ClaimGroup.id)
        )
    ).all()
    for group in groups:
        await claims_uc.promote_to_n4(
            session,
            actor,
            group=group,
            content=content,
            requested_by=requested_by,
            language_not_processed=language_not_processed,
        )


async def _reuse_existing(
    session: AsyncSession,
    actor: Actor,
    content: ContentItem,
    level: str,
    requested_by: str,
    language_not_processed: bool,
) -> bool:
    """True when the content already has (or inherits) claims: no extraction. Serialized
    per content so two jobs cannot both extract."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"factcheck:extract:{content.id}"},
    )
    claim_ids = list(
        await session.scalars(
            select(ContentClaim.claim_id)
            .where(ContentClaim.content_id == content.id)
            .order_by(ContentClaim.position)
        )
    )
    if not claim_ids:
        earlier = await session.scalar(
            select(ContentItem.id)
            .where(
                ContentItem.content_hash == content.content_hash,
                ContentItem.id < content.id,
                exists().where(ContentClaim.content_id == ContentItem.id),
            )
            .order_by(ContentItem.id)
            .limit(1)
        )
        if earlier is None:
            return False
        rows = (
            await session.execute(
                select(ContentClaim.claim_id, ContentClaim.position).where(
                    ContentClaim.content_id == earlier
                )
            )
        ).all()
        await session.execute(
            pg_insert(ContentClaim)
            .values([{"content_id": content.id, "claim_id": r[0], "position": r[1]} for r in rows])
            .on_conflict_do_nothing()
        )
        claim_ids = [r[0] for r in rows]
        log.info("factcheck.copy_linked", content=content.id, original=earlier)
    if level == "N4":
        await _promote_groups(
            session, actor, content, claim_ids, requested_by, language_not_processed
        )
    return True


async def _run_extract(
    ctx: JobContext, level: str, payload: dict[str, Any]
) -> tuple[ExtractOutput, str] | None:
    request = LLMRequest(
        role=names.ROLE_EXTRACT,
        level=Level(level),
        prompt_id=names.prompt_id(names.ROLE_EXTRACT),
        input=payload,
        schema=ExtractOutput,
        max_output_tokens=_EXTRACT_MAX_TOKENS,
        budget_scope=names.level_scope(level),
    )
    try:
        result = await _complete(ctx, request)
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, stage="extract")
        return None
    output = result.output
    if not isinstance(output, ExtractOutput):
        raise TypeError("unexpected extraction output type")
    return output, result.prompt_version


async def extract(ctx: JobContext) -> None:
    payload = ctx.job.payload
    try:
        content_id = int(payload["content_id"])
        level = str(payload["level"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError("bad extract payload") from exc
    if level not in ("N3", "N4"):
        raise PermanentError(f"unsupported extraction level {level!r}")
    requested_by = str(payload.get("requested_by") or names.FACTCHECK_ACTOR)
    actor = _actor()
    settings = ctx.settings

    async with ctx.db.transaction() as session:
        content = await session.get(ContentItem, content_id)
        if content is None:
            log.info("factcheck.content_missing", content=content_id)
            return
        unprocessed = content.lang not in settings.fc_language_list
        if level == "N3" and unprocessed:
            log.info("factcheck.language_skipped", content=content_id, lang=content.lang)
            return
        if await _reuse_existing(session, actor, content, level, requested_by, unprocessed):
            return
        post_text, post_lang, created = content.text, content.lang, content.created_at_platform
        context_kind, context_text = await _thread_text(session, content)

    model_input = inputs.extract_input(
        post_id=str(content_id),
        lang=post_lang,
        text=post_text,
        created_at=created,
        today=datetime.now(UTC).date(),
        context_kind=context_kind,
        context_text=context_text or None,
    )
    first = await _run_extract(ctx, level, model_input)
    if first is None:
        return
    output, prompt_version = first
    _check_post_id(output, content_id)
    claims, failed = _faithful(output, post_text, context_text, content_id)
    if failed:
        replay = await _run_extract(ctx, level, {**model_input, "replay": REPLAY_NOTE})
        if replay is None:
            return
        output, prompt_version = replay
        _check_post_id(output, content_id)
        claims, _ = _faithful(output, post_text, context_text, content_id)

    try:
        decisions = await matching.match_claims(
            ctx,
            content_id=content_id,
            post_date=created.date() if created is not None else None,
            lang=post_lang,
            claims=claims,
            level=level,
        )
    except BudgetExhausted as exc:
        raise RetryLater(_retry_delay(exc.retry_at), "model budget exhausted") from exc

    async with ctx.db.transaction() as session:
        content = await session.get(ContentItem, content_id)
        if content is None:
            return
        if await _reuse_existing(session, actor, content, level, requested_by, unprocessed):
            return
        profile = await active_profile(session, settings)
        record = await claims_uc.record_extraction(
            session,
            actor,
            content=content,
            claims=claims,
            level=level,
            requested_by=requested_by,
            prompt_version=prompt_version,
            thread_context=output.thread_context,
            absence_reason=output.absence_reason.value if output.absence_reason else None,
            language_not_processed=unprocessed,
            matches=decisions if claims else None,
            post_date=created.date() if created is not None else None,
            profile=profile,
            current_period_days=settings.fc_current_period_days,
        )
    log.info(
        "factcheck.extracted",
        content=content_id,
        level=level,
        claims=len(record.claim_ids),
        investigations=len(record.investigation_ids),
    )


def _check_post_id(output: ExtractOutput, content_id: int) -> None:
    if output.post_id != str(content_id):
        capture(ec.FCK_MODEL_OUTPUT_INVALID, message="Extraction returned another post id")
        raise RuntimeError("extraction post_id does not match the input")


# ------------------------------------------------------------- manual request (N4)


def _to_item_input(item: PlatformItem) -> ItemInput:
    author = None
    if item.author_external_id is not None or item.author_handle is not None:
        author = AuthorInput(external_id=item.author_external_id, handle=item.author_handle)
    return ItemInput(
        platform=item.platform,
        external_id=item.external_id,
        text=item.text,
        author=author,
        lang_hint=item.lang_hint,
        created_at=item.created_at,
        metrics=dict(item.metrics.values) if item.metrics is not None else {},
        parent_external_id=item.parent_external_id,
        quoted_external_id=item.quoted_external_id,
        media=tuple(MediaInput(kind=m.kind, url=m.url) for m in item.media),
    )


async def request(ctx: JobContext) -> None:
    payload = ctx.job.payload
    try:
        platform = str(payload["platform"])
        external_id = str(payload["external_id"])
    except KeyError as exc:
        raise PermanentError("bad request payload") from exc
    requested_by = str(payload.get("requested_by") or names.FACTCHECK_ACTOR)
    try:
        item = await asm.read_item(
            ctx.db,
            ctx.settings,
            platform,
            external_id,
            ref=f"factcheck:request:{platform}:{external_id}",
        )
    except PlatformNotConfigured as exc:
        capture(ec.RAD_NOT_CONFIGURED, exc, platform=platform)
        raise PermanentError("platform not configured") from exc
    except BudgetRefused as exc:
        raise RetryLater(_retry_delay(exc.retry_at), "platform budget exhausted") from exc
    except PlatformRateLimited as exc:
        raise RetryLater(max(_MIN_RETRY_S, exc.retry_after), "platform rate limited") from exc
    except PlatformUnavailable as exc:
        raise RetryLater(_MIN_RETRY_S, "platform unavailable") from exc
    except (PlatformError, UnsupportedOperation) as exc:
        capture(ec.FCK_PLATFORM_UNAVAILABLE, exc, platform=platform)
        raise
    if item is None:
        capture(
            ec.FCK_CONTENT_NOT_FOUND,
            message="Requested post not found",
            platform=platform,
            external_id=external_id,
        )
        raise PermanentError("content not found on the platform")

    async with ctx.db.transaction() as session:
        result = await ingest_items(
            session,
            _actor(),
            [_to_item_input(item)],
            source="manual",
            filter_version=None,
            installation_token=None,
            max_items=1,
            daily_quota=ctx.settings.ingest_daily_quota,
        )
        if result.accepted == 0:
            raise PermanentError("the requested post was rejected by ingestion")
        content_id = await session.scalar(
            select(ContentItem.id).where(
                ContentItem.platform == item.platform, ContentItem.external_id == item.external_id
            )
        )
        if content_id is None:  # pragma: no cover (just upserted)
            raise PermanentError("content missing after ingestion")
        await enqueue(
            session,
            QUEUE,
            names.JOB_EXTRACT,
            {"content_id": content_id, "level": "N4", "requested_by": requested_by},
            dedupe_key=names.extract_key(content_id, "N4"),
        )
    log.info("factcheck.requested", content=content_id, platform=platform)


# ------------------------------------------------------------------- E10: assembly


async def assemble(ctx: JobContext) -> None:
    try:
        investigation_id = int(ctx.job.payload["investigation_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError("bad assemble payload") from exc
    settings = ctx.settings

    async with ctx.db.session() as session:
        investigation = await session.get(Investigation, investigation_id)
        if investigation is None or investigation.stage != "completed":
            log.info("factcheck.assemble_skipped", investigation=investigation_id)
            return
        verdict = await session.scalar(
            select(Verdict).where(Verdict.investigation_id == investigation_id)
        )
        confidence: float | None = None
        band_value: str | None = None
        recheck_target: tuple[str, str, bytes] | None = None
        if verdict is not None:
            group = await session.get(ClaimGroup, verdict.claim_group_id)
            if group is None:  # pragma: no cover (foreign key)
                raise PermanentError("verdict without claim group")
            claim_type = await session.scalar(
                text("SELECT claim_type FROM claims WHERE id = :i"),
                {"i": group.representative_claim_id},
            )
            confidence_value, band_enum = await asm.final_confidence(
                session, settings, verdict, investigation, str(claim_type)
            )
            confidence, band_value = confidence_value, band_enum.value
            if investigation.content_id is not None:
                content = await session.get(ContentItem, investigation.content_id)
                if content is not None:
                    recheck_target = (content.platform, content.external_id, content.content_hash)

    new_flags: dict[str, Any] = {}
    if verdict is not None:
        if recheck_target is None:
            new_flags["recheck"] = "skipped"
        else:
            platform, external_id, stored_hash = recheck_target
            new_flags.update(
                await asm.recheck_flags(
                    ctx.db,
                    settings,
                    platform=platform,
                    external_id=external_id,
                    stored_hash=stored_hash,
                    investigation_id=investigation_id,
                )
            )

    async with ctx.db.transaction() as session:
        locked = await session.scalar(
            select(Investigation).where(Investigation.id == investigation_id).with_for_update()
        )
        if locked is None or locked.stage != "completed":
            return
        locked_verdict = (
            await session.scalar(
                select(Verdict)
                .where(Verdict.investigation_id == investigation_id)
                .with_for_update()
            )
            if verdict is not None
            else None
        )
        if locked_verdict is not None and locked_verdict.status == "draft":
            flags = dict(locked_verdict.flags or {})
            flags.update(new_flags)
            flags["confidence_band"] = band_value
            locked_verdict.confidence = confidence
            locked_verdict.flags = flags
            await asm.lock_quota(session)
            used = await asm.today_count(session)
            quota = (await active_profile(session, settings)).draft_daily_quota
            if used < quota:
                await assembly_uc.open_draft(session, _actor(), locked_verdict, locked)
            else:
                flags["over_quota_since"] = datetime.now(UTC).isoformat()
                locked_verdict.flags = flags
                capture(
                    ec.FCK_DRAFT_QUOTA_REACHED,
                    quota=quota,
                    investigation=investigation_id,
                )
        locked.stage = "assembled"
        locked.updated_at = datetime.now(UTC)
    log.info("factcheck.assembled", investigation=investigation_id)


# ------------------------------------------------------------------- translations


def _source_row(
    rows: dict[str, ContextNoteTranslation], retranslate_from: str | None
) -> ContextNoteTranslation | None:
    if retranslate_from is not None and retranslate_from in rows:
        return rows[retranslate_from]
    reviewed = [
        r
        for r in rows.values()
        if r.origin in (TranslationOrigin.HUMAN.value, TranslationOrigin.ORIGINAL.value)
    ]
    if not reviewed:
        return None

    def rank(r: ContextNoteTranslation) -> tuple[int, int, str]:
        lang_rank = _REFERENCE_LANGS.index(r.lang) if r.lang in _REFERENCE_LANGS else 2
        return (0 if r.origin == TranslationOrigin.HUMAN.value else 1, lang_rank, r.lang)

    return min(reviewed, key=rank)


async def translate(ctx: JobContext) -> None:
    payload = ctx.job.payload
    try:
        verdict_public = uuid.UUID(str(payload["verdict_id"]))
    except (KeyError, ValueError) as exc:
        raise PermanentError("bad translate payload") from exc
    retranslate_from = payload.get("retranslate_from")
    retranslate_from = str(retranslate_from) if retranslate_from else None

    async with ctx.db.session() as session:
        verdict = await session.scalar(select(Verdict).where(Verdict.public_id == verdict_public))
        if verdict is None or verdict.status != "active":
            log.info("factcheck.translate_skipped", verdict=str(verdict_public))
            return
        note = await session.scalar(
            select(ContextNote).where(ContextNote.verdict_id == verdict.id).order_by(ContextNote.id)
        )
        group = await session.get(ClaimGroup, verdict.claim_group_id)
        if note is None or group is None:
            return
        rows = {
            r.lang: r
            for r in (
                await session.scalars(
                    select(ContextNoteTranslation).where(ContextNoteTranslation.note_id == note.id)
                )
            ).all()
        }
        note_id, level = note.id, Level(group.level)
        source = _source_row(rows, retranslate_from)
        if source is None:
            log.info("factcheck.translate_no_source", verdict=str(verdict_public))
            return
        src_lang, src_text, src_sheet = source.lang, source.text, source.fact_sheet
        targets = sorted({*_REFERENCE_LANGS, note.lang} - {src_lang})
        todo = [
            lang
            for lang in targets
            if lang not in rows
            or (
                retranslate_from is not None
                and rows[lang].origin == TranslationOrigin.MACHINE.value
            )
        ]

    for lang in todo:
        try:
            text_out = await _translate_one(ctx, level, src_text, src_lang, lang, "context_note")
            sheet_out = (
                await _translate_one(ctx, level, src_sheet, src_lang, lang, "fact_sheet")
                if src_sheet
                else None
            )
        except (RetryLater, PermanentError):
            raise
        except Exception as exc:
            capture(ec.FCK_TRANSLATION_FAILED, exc, verdict=str(verdict_public), target=lang)
            raise
        async with ctx.db.transaction() as session:
            await assembly_uc.store_machine_translations(
                session, _actor(), note_id=note_id, items=[(lang, text_out, sheet_out)]
            )
    log.info("factcheck.translated", verdict=str(verdict_public), languages=len(todo))


async def _translate_one(
    ctx: JobContext, level: Level, source_text: str, src_lang: str, target: str, kind: str
) -> str:
    request = LLMRequest(
        role=names.ROLE_TRANSLATE,
        level=level,
        prompt_id=names.prompt_id(names.ROLE_TRANSLATE),
        input=inputs.translate_input(
            text=source_text, source_lang=src_lang, target_lang=target, kind=kind
        ),
        schema=TranslateOutput,
        max_output_tokens=_NOTE_MAX_TOKENS if kind == "context_note" else _SHEET_MAX_TOKENS,
        budget_scope=names.level_scope(level.value),
    )
    result = await _complete(ctx, request)
    if not isinstance(result.output, TranslateOutput):
        raise TypeError("unexpected translation output type")
    return result.output.text


# ------------------------------------------------------------------- subscriptions


async def on_content_ingested(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """Enqueue the extraction of new contents written by an active watched account (N3),
    or by an account carrying an active ``account`` level override at N3 or N4 (the
    override level; no need to be watched)."""
    languages = set(get_settings().fc_language_list)
    in_language: dict[int, bool] = {}
    for event in events:
        payload = event.payload
        try:
            content_id = int(payload["id"])
        except (KeyError, TypeError, ValueError):
            continue
        in_language[content_id] = str(payload.get("lang", "")).lower() in languages
    if not in_language:
        return
    ids = [i for i, ok in in_language.items() if ok]
    queued: set[int] = set()
    if ids:
        watched = await session.scalars(
            select(ContentItem.id)
            .join(WatchedAccount, WatchedAccount.account_id == ContentItem.author_account_id)
            .where(
                ContentItem.id.in_(ids),
                WatchedAccount.active.is_(True),
                ContentItem.copy_of_id.is_(None),
            )
            .order_by(ContentItem.id)
        )
        for content_id in watched:
            queued.add(content_id)
            await enqueue(
                session,
                QUEUE,
                names.JOB_EXTRACT,
                {"content_id": content_id, "level": "N3", "requested_by": names.FACTCHECK_ACTOR},
                dedupe_key=names.extract_key(content_id, "N3"),
            )
    authored = (
        await session.execute(
            select(ContentItem.id, ContentItem.author_account_id)
            .where(
                ContentItem.id.in_(list(in_language)),
                ContentItem.author_account_id.is_not(None),
                ContentItem.copy_of_id.is_(None),
            )
            .order_by(ContentItem.id)
        )
    ).all()
    for content_id, account_id in authored:
        if content_id in queued:
            continue
        level = await override_level(
            session, content_id=content_id, group_ids=[], account_id=account_id
        )
        if level not in ("N3", "N4") or (level == "N3" and not in_language[content_id]):
            continue
        await enqueue(
            session,
            QUEUE,
            names.JOB_EXTRACT,
            {"content_id": content_id, "level": level, "requested_by": names.FACTCHECK_ACTOR},
            dedupe_key=names.extract_key(content_id, level),
        )


async def on_investigation_completed(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """Enqueue the E10 assembly of each completed investigation."""
    public_ids: list[uuid.UUID] = []
    for event in events:
        if event.payload.get("outcome") == "failed":
            continue
        try:
            public_ids.append(uuid.UUID(str(event.payload["investigation_id"])))
        except (KeyError, ValueError):
            continue
    if not public_ids:
        return
    rows = await session.scalars(
        select(Investigation.id)
        .where(Investigation.public_id.in_(public_ids))
        .order_by(Investigation.id)
    )
    for investigation_id in rows:
        await enqueue(
            session,
            QUEUE,
            names.JOB_ASSEMBLE,
            {"investigation_id": investigation_id},
            dedupe_key=names.assemble_key(investigation_id),
        )


async def on_verdict_published(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """Enqueue the machine translations of each published verdict."""
    for event in events:
        try:
            public_id = uuid.UUID(str(event.payload["verdict_id"]))
        except (KeyError, ValueError):
            continue
        row = (
            await session.execute(
                select(Verdict.id, Verdict.version).where(Verdict.public_id == public_id)
            )
        ).first()
        if row is None:
            continue
        await enqueue(
            session,
            QUEUE,
            names.JOB_TRANSLATE,
            {
                "verdict_id": str(public_id),
                "retranslate_from": event.payload.get("retranslate_from"),
            },
            dedupe_key=names.translate_key(int(row[0]), int(row[1])),
        )
