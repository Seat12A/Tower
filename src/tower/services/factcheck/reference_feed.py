"""INSEE reference-fact feed: periodic task ``factcheck:reference_feed`` and job
``factcheck.insee_refresh`` (queue fc-bulk).

``settings.fc_insee_indicators`` is a JSON list of tracked series (see
docs/configuration.md). Each day one job per indicator reads the latest observation
through the INSEE adapter and records it as a ``pending`` reference fact: a reviewer must
validate it before it serves. The adapter only exposes ``lookup``: its ``Evidence`` is
parsed (value and period of the newest observation); an evidence that cannot be parsed is
skipped with a log.
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import DomainError
from tower.core.domain.factcheck.values import FactOrigin
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.logging import get_logger
from tower.core.ports.knowledge import Evidence, SourceQuery
from tower.core.services.base import JobContext
from tower.core.usecases import references as references_uc

if TYPE_CHECKING:
    from tower.core.ports.knowledge import KnowledgeSource

__all__ = [
    "Indicator",
    "enqueue_refreshes",
    "insee_refresh",
    "parse_indicators",
    "set_source_factory",
]

log = get_logger(__name__)

_RETRY_S = 3600.0
_IDBANK = re.compile(r"[0-9]{9}")
_PERIOD = re.compile(r"^(\d{4})(?:-(Q[1-4]|\d{2}))?$")

SourceFactory = Callable[[Settings, Database, "Indicator"], Awaitable["KnowledgeSource"]]
_factory: SourceFactory | None = None
_warned_raw: str | None = None


@dataclass(frozen=True, slots=True)
class Indicator:
    indicator: str
    label: str
    idbank: str
    unit: str
    territory: str
    definition_variant: str | None
    subject_qid: str | None
    aliases: tuple[str, ...]
    period: str
    source_name: str


def set_source_factory(factory: SourceFactory | None) -> None:
    """Replace the INSEE source builder (tests); ``None`` restores the default."""
    global _factory
    _factory = factory


def _text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_indicators(raw: str) -> list[Indicator]:
    """Tracked indicators. Invalid JSON disables the feed (logged once); an invalid entry
    is skipped."""
    global _warned_raw
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("expected a JSON list")
    except ValueError as exc:
        if _warned_raw != raw:
            _warned_raw = raw
            log.warning("factcheck.insee_config_invalid", error=str(exc)[:200])
        return []
    out: list[Indicator] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        indicator, label = _text(entry, "indicator"), _text(entry, "label")
        idbank, unit = _text(entry, "idbank"), _text(entry, "unit")
        territory = _text(entry, "territory")
        if not (indicator and label and idbank and unit and territory):
            _warn_entry(raw, indicator)
            continue
        if not _IDBANK.fullmatch(idbank):
            _warn_entry(raw, indicator)
            continue
        aliases = entry.get("aliases")
        out.append(
            Indicator(
                indicator=indicator,
                label=label,
                idbank=idbank,
                unit=unit,
                territory=territory,
                definition_variant=_text(entry, "definition_variant"),
                subject_qid=_text(entry, "subject_qid"),
                aliases=tuple(a for a in aliases if isinstance(a, str))
                if isinstance(aliases, list)
                else (),
                period=_text(entry, "period") or "quarter",
                source_name=_text(entry, "source_name") or "INSEE",
            )
        )
    return out


def _warn_entry(raw: str, indicator: str | None) -> None:
    global _warned_raw
    if _warned_raw != raw:
        _warned_raw = raw
        log.warning("factcheck.insee_entry_invalid", indicator=indicator)


async def enqueue_refreshes(session: AsyncSession, settings: Settings) -> int:
    """Periodic ``factcheck:reference_feed`` (daily): one refresh job per indicator."""
    today = datetime.now(UTC).date().isoformat()
    count = 0
    for item in parse_indicators(settings.fc_insee_indicators):
        job_id = await enqueue(
            session,
            names.FC_BULK,
            names.JOB_INSEE_REFRESH,
            {"indicator": item.indicator},
            dedupe_key=names.insee_key(item.indicator, today),
        )
        count += job_id is not None
    return count


def _period_dates(period: str) -> tuple[date, date] | None:
    match = _PERIOD.fullmatch(period)
    if match is None:
        return None
    year, part = int(match.group(1)), match.group(2)
    if part is None:
        return date(year, 1, 1), date(year, 12, 31)
    if part.startswith("Q"):
        first = (int(part[1]) - 1) * 3 + 1
        return date(year, first, 1), date(year, first + 2, monthrange(year, first + 2)[1])
    month = int(part)
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def parse_evidence(evidence: Evidence) -> tuple[Decimal, str] | None:
    """(value, period) of the newest observation of an INSEE ``Evidence``, or ``None``."""
    periods = evidence.extra.get("periods", "")
    period = periods.split(",")[0].strip() if periods else ""
    if not period or _period_dates(period) is None:
        return None
    first_line = evidence.text.splitlines()[0] if evidence.text else ""
    match = re.search(rf":\s*(-?\d+(?:\.\d+)?)[^()]*\({re.escape(period)}\)", first_line)
    if match is None:
        return None
    try:
        return Decimal(match.group(1)), period
    except InvalidOperation:  # pragma: no cover - regex guarantees a number
        return None


async def _default_source(settings: Settings, db: Database, item: Indicator) -> KnowledgeSource:
    from tower.core.adapters.knowledge.insee import InseeSource

    del db
    return InseeSource(
        series={item.label: item.idbank},
        user_agent=settings.http_user_agent,
        timeout=settings.atlas_http_timeout_s,
    )


async def _latest(
    settings: Settings, db: Database, item: Indicator
) -> tuple[Evidence, Decimal, str] | None:
    source = await (_factory or _default_source)(settings, db, item)
    try:
        evidences = await source.lookup(SourceQuery(text=item.label, lang="fr"))
    finally:
        closer = getattr(source, "aclose", None)
        if closer is not None:
            await closer()
    for evidence in evidences:
        if evidence.stable_id != item.idbank:
            continue
        parsed = parse_evidence(evidence)
        if parsed is not None:
            return evidence, parsed[0], parsed[1]
    return None


async def insee_refresh(ctx: JobContext) -> None:
    """Job ``factcheck.insee_refresh`` ``{"indicator": str}``: latest observation of the
    series as a pending fact (idempotent on ``insee:<idbank>:<period>``)."""
    key = str(ctx.job.payload.get("indicator", ""))
    item = next(
        (i for i in parse_indicators(ctx.settings.fc_insee_indicators) if i.indicator == key),
        None,
    )
    if item is None:
        log.info("factcheck.insee_refresh_skipped", indicator=key, reason="not_configured")
        return
    try:
        found = await _latest(ctx.settings, ctx.db, item)
        if found is None:
            log.info("factcheck.insee_refresh_skipped", indicator=key, reason="no_observation")
            return
        evidence, value, period = found
        bounds = _period_dates(period)
        if bounds is None:  # pragma: no cover - checked by parse_evidence
            return
        published = (evidence.published_at or datetime.now(UTC)).date()
        async with ctx.db.transaction() as session:
            await references_uc.create_reference_fact(
                session,
                Actor.system(names.FACTCHECK_ACTOR),
                indicator=item.indicator,
                label=item.label,
                value=value,
                unit=item.unit,
                period_start=bounds[0],
                period_end=bounds[1],
                territory=item.territory,
                source_url=evidence.url,
                source_name=item.source_name,
                published_at=published,
                definition_variant=item.definition_variant,
                subject_qid=item.subject_qid,
                aliases=item.aliases,
                origin=FactOrigin.INSEE,
                external_key=f"insee:{item.idbank}:{period}",
            )
    except DomainError as exc:
        capture(ec.FCK_REFERENCE_FEED_FAILED, exc, indicator=key)
        raise PermanentError(f"invalid reference fact for {key}") from exc
    except Exception as exc:
        capture(ec.FCK_REFERENCE_FEED_FAILED, exc, indicator=key)
        if ctx.job.attempts >= ctx.job.max_attempts:
            raise PermanentError(f"INSEE refresh failed for {key}") from exc
        raise RetryLater(_RETRY_S, "INSEE refresh failed") from exc
