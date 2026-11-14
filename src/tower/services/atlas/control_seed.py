"""Seeding of ``source_registry.controlled_by`` from Wikidata (job ``atlas.control_seed``).

For a registry entry with a ``qid``, reads the statements "parent organization" (P749) and
"owned by" (P127) of the publisher through the Wikidata Action API (``wbgetentities``; the
repo's Wikidata adapter avoids SPARQL because of the main/scholarly graph split), keeps the
relations that have not ended (qualifier P582 "end time"), prefers P749 over P127 and records
every label found (fr/en) as ``controlled_by_aliases``. A value set by the team or an
official register is never overwritten (``apply_control_seed``).

Resolving the QID from the domain (property P856) is NOT done: the QID is set by editors.
An entry without QID is skipped.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.adapters.knowledge.cache import DbSourceCache
from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.models.factcheck import SourceRegistryEntry
from tower.core.ports.knowledge import SourceCache, SourceUnavailable
from tower.core.services.base import JobContext
from tower.core.usecases.registry import apply_control_seed

__all__ = ["Relation", "control_seed", "enqueue_control_seeds", "pick_controller"]

API = "https://www.wikidata.org/w/api.php"
NAME = "wikidata"
PARENT = "P749"
OWNER = "P127"
END_TIME = "P582"
RETRY_S = 3600
MAX_PER_RUN = 50
_QID = re.compile(r"^Q[1-9][0-9]{0,15}$")


@dataclass(frozen=True, slots=True)
class Relation:
    """One statement P749/P127 of the publisher."""

    prop: str
    qid: str
    labels: tuple[str, ...]
    """Labels (fr first, then en) of the target entity."""
    ended: bool = False
    preferred: bool = False


RelationsFetch = Callable[[str], Awaitable[list[Relation]]]


def pick_controller(relations: list[Relation]) -> tuple[Relation | None, list[str]]:
    """Chosen controller (P749 before P127, running before ended, preferred rank first)
    and the aliases: every label of every relation except the chosen name."""
    running = [r for r in relations if not r.ended and r.labels]
    pool = [r for r in running if r.prop == PARENT] or [r for r in running if r.prop == OWNER] or []
    chosen = sorted(pool, key=lambda r: not r.preferred)[0] if pool else None
    if chosen is None:
        return None, []
    aliases: list[str] = []
    for rel in (chosen, *(r for r in relations if r is not chosen)):
        for label in rel.labels:
            if label != chosen.labels[0] and label not in aliases:
                aliases.append(label)
    return chosen, aliases


def _label(entity: Mapping[str, Any], lang: str) -> str | None:
    labels = entity.get("labels")
    item = labels.get(lang) if isinstance(labels, dict) else None
    value = item.get("value") if isinstance(item, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _target_qid(claim: Mapping[str, Any]) -> str | None:
    snak = claim.get("mainsnak")
    if not isinstance(snak, dict) or snak.get("snaktype") != "value":
        return None
    value = (snak.get("datavalue") or {}).get("value")
    qid = value.get("id") if isinstance(value, dict) else None
    return qid if isinstance(qid, str) and _QID.fullmatch(qid) else None


def make_fetch(http: SourceHttp, cache: SourceCache | None, ttl_s: int) -> RelationsFetch:
    """Default fetcher: two cached Action API calls (claims, then labels of the targets)."""

    async def entities(ids: list[str], props: str) -> dict[str, Any]:
        request = {"op": "control", "ids": sorted(ids), "props": props}

        async def fetch() -> dict[str, Any]:
            body = await http.json_dict(
                "GET",
                API,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(ids),
                    "props": props,
                    "languages": "fr|en",
                    "format": "json",
                    "formatversion": 2,
                },
            )
            found = body.get("entities")
            if not isinstance(found, dict):
                raise http.fail("wikidata: entities response", ec.ATL_BAD_RESPONSE)
            slim: dict[str, Any] = {}
            for qid, ent in found.items():
                if not isinstance(ent, dict) or "missing" in ent:
                    continue
                claims = ent.get("claims") or {}
                slim[str(qid)] = {
                    "labels": ent.get("labels") or {},
                    "claims": {p: claims[p] for p in (PARENT, OWNER) if p in claims},
                }
            return {"entities": slim}

        result = await cached(cache, NAME, request, ttl_s, fetch)
        out = result.get("entities", {})
        return out if isinstance(out, dict) else {}

    async def fetch_relations(qid: str) -> list[Relation]:
        ent = (await entities([qid], "claims")).get(qid)
        if not isinstance(ent, dict):
            return []
        raw: list[tuple[str, str, bool, bool]] = []
        for prop in (PARENT, OWNER):
            for claim in ent.get("claims", {}).get(prop, []):
                if not isinstance(claim, dict) or claim.get("rank") == "deprecated":
                    continue
                target = _target_qid(claim)
                if target is None:
                    continue
                ended = END_TIME in (claim.get("qualifiers") or {})
                raw.append((prop, target, ended, claim.get("rank") == "preferred"))
        if not raw:
            return []
        targets = sorted({t for _, t, _, _ in raw})[:50]
        labelled = await entities(targets, "labels")
        out: list[Relation] = []
        for prop, target, ended, preferred in raw:
            info = labelled.get(target)
            if not isinstance(info, dict):
                continue
            labels = tuple(dict.fromkeys(x for x in (_label(info, "fr"), _label(info, "en")) if x))
            if labels:
                out.append(Relation(prop, target, labels, ended, preferred))
        return out

    return fetch_relations


async def control_seed(ctx: JobContext, *, fetch: RelationsFetch | None = None) -> None:
    """Job ``atlas.control_seed``: payload ``{"registry_id": "…"}``."""
    try:
        registry_id = int(ctx.job.payload["registry_id"])
    except (KeyError, TypeError, ValueError):
        raise PermanentError("atlas.control_seed: registry_id is missing") from None
    async with ctx.db.session() as session:
        entry = await session.get(SourceRegistryEntry, registry_id)
        if entry is None or not entry.active:
            raise PermanentError("atlas.control_seed: unknown or inactive entry")
        qid = entry.qid
    if not qid:
        ctx.log.info("atlas.control_seed.skipped", registry_id=registry_id, reason="no_qid")
        return
    http: SourceHttp | None = None
    if fetch is None:
        http = SourceHttp(
            NAME, timeout=ctx.settings.atlas_http_timeout_s, user_agent=ctx.settings.http_user_agent
        )
        fetch = make_fetch(http, DbSourceCache(ctx.db), ctx.settings.atlas_cache_ttl_s)
    try:
        relations = await fetch(qid)
    except SourceUnavailable as exc:
        capture(ec.ATL_CONTROL_SEED_FAILED, exc, registry_id=registry_id, qid=qid)
        raise RetryLater(RETRY_S, "wikidata unavailable") from exc
    finally:
        if http is not None:
            await http.aclose()
    chosen, aliases = pick_controller(relations)
    async with ctx.db.transaction() as session:
        written = await apply_control_seed(
            session,
            Actor.system("atlas"),
            registry_id,
            controlled_by=chosen.labels[0] if chosen else None,
            controlled_by_qid=chosen.qid if chosen else None,
            aliases=aliases,
        )
    ctx.log.info(
        "atlas.control_seed.done",
        registry_id=registry_id,
        controller=chosen.qid if chosen else None,
        written=written,
    )


async def enqueue_control_seeds(session: AsyncSession, settings: Settings) -> int:
    """Weekly: queue the seed of active entries with a QID and no control origin yet
    (at most 50 per run). Returns the number of jobs queued."""
    ids = (
        await session.scalars(
            select(SourceRegistryEntry.id)
            .where(
                SourceRegistryEntry.active.is_(True),
                SourceRegistryEntry.qid.is_not(None),
                SourceRegistryEntry.control_origin.is_(None),
            )
            .order_by(func.random())  # rotate: entries without a controller stay NULL
            .limit(MAX_PER_RUN)
        )
    ).all()
    queued = 0
    for registry_id in ids:
        job = await enqueue(
            session,
            names.ATLAS_QUEUE,
            names.JOB_CONTROL_SEED,
            {"registry_id": str(registry_id)},
            dedupe_key=names.control_seed_key(registry_id),
        )
        queued += job is not None
    return queued
