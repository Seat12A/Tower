"""Names shared by the fact-check services, gate, the bot and the docs: queues, job
kinds, topics, dedupe keys, budget scopes, model roles and prompt ids."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Queues
FC_FAST = "fc-fast"
FC_DEEP = "fc-deep"
FC_BULK = "fc-bulk"
# Fast lane: N3/N4 work and the investigation of a surging narrative jump the queues
# (one worker serves several queues in priority order; mass-flow jobs keep priority 0).
FAST_LANE_PRIORITY = 10


def job_priority(level: str | None, flags: Mapping[str, Any] | None = None) -> int:
    """Queue priority of a fact-check job from the level and flags of its work."""
    if level in ("N3", "N4") or (flags or {}).get("wave"):
        return FAST_LANE_PRIORITY
    return 0


# Jobs of the service ``factcheck`` (queue fc-fast)
JOB_EXTRACT = "factcheck.extract"
JOB_ASSEMBLE = "factcheck.assemble"
JOB_REQUEST = "factcheck.request"
JOB_TRANSLATE = "factcheck.translate"

# Lot 4 jobs of the service ``factcheck`` (queue fc-bulk)
JOB_EMBED_BATCH = "factcheck.embed_batch"
JOB_REVALIDATE = "factcheck.revalidate"
JOB_INSEE_REFRESH = "factcheck.insee_refresh"

# Lot 5 jobs (queue fc-bulk unless stated)
JOB_EXTRACT_BATCH = "factcheck.extract_batch"
JOB_LIGHT_CHECK = "factcheck.light_check"
JOB_ATTACH_NARRATIVE = "factcheck.attach_narrative"
JOB_NARRATIVE_TITLE = "factcheck.narrative_title"
JOB_PROMOTION_CHECK = "factcheck.promotion_check"  # platform read confirming N2 metrics

# Jobs of the service ``investigate`` (queue fc-deep), one per stage
JOB_FRAME = "investigate.frame"
JOB_COLLECT = "investigate.collect"
JOB_JUDGE = "investigate.judge"
JOB_SECOND_OPINION = "investigate.second_opinion"
JOB_OBJECTION = "investigate.objection"
JOB_ESCALATE = "investigate.escalate"
JOB_DRAFT = "investigate.draft"

# Topics
CONTENT_INGESTED = "content.ingested"
CLAIM_EXTRACTED = "claim.extracted"
INVESTIGATION_REQUESTED = "investigation.requested"
INVESTIGATION_COMPLETED = "investigation.completed"
DRAFT_READY = "draft.ready"
DRAFT_REVIEWED = "draft.reviewed"
VERDICT_PUBLISHED = "verdict.published"
# Lot 4
CLAIM_GROUP_CREATED = "claim_group.created"
CLAIM_GROUP_MERGED = "claim_group.merged"
CLAIM_GROUP_SPLIT = "claim_group.split"
CLAIM_GROUP_LEVEL_CHANGED = "claim_group.level_changed"
VERDICT_REUSED = "verdict.reused"
VERDICT_EXPIRED = "verdict.expired"
REFERENCE_FACT_VALIDATED = "reference_fact.validated"
VERDICT_BAND_VALIDATED = "verdict_band.validated"
PROCESSING_PROFILE_ACTIVATED = "processing_profile.activated"
LEVEL_OVERRIDE_CREATED = "level_override.created"
# Lot 5
NARRATIVE_SURGE_DETECTED = "narrative.surge_detected"
NARRATIVE_SURGE_ENDED = "narrative.surge_ended"
NARRATIVE_MERGED = "narrative.merged"
NARRATIVE_SPLIT = "narrative.split"

# Actors
FACTCHECK_ACTOR = "system:factcheck"
INVESTIGATE_ACTOR = "system:investigate"

# Model roles (routes in llm_routes) and prompt ids
ROLE_EXTRACT = "extract"
ROLE_FRAME = "frame"
ROLE_FRAME_REINFORCED = "frame_reinforced"
ROLE_QUERY_WRITER = "query_writer"
ROLE_JUDGE = "judge"
ROLE_JUDGE_REINFORCED = "judge_reinforced"
ROLE_SECOND_OPINION = "second_opinion"
ROLE_OBJECTION = "objection"
ROLE_WRITER = "writer"
ROLE_CRITIC = "critic"
ROLE_TRANSLATE = "translate"
ROLE_PAIR_ARBITER = "pair_arbiter"
ROLE_EMBED = "embed"
ROLE_EXTRACT_BATCH = "extract_batch"
ROLE_LIGHT_CHECK = "light_check"
ROLE_NARRATIVE_TITLE = "narrative_title"
"""Prompt id = role, except the reinforced roles which reuse ``frame`` and ``judge``."""

PROMPT_OF_ROLE = {
    ROLE_FRAME_REINFORCED: ROLE_FRAME,
    ROLE_JUDGE_REINFORCED: ROLE_JUDGE,
}


def prompt_id(role: str) -> str:
    return PROMPT_OF_ROLE.get(role, role)


def level_scope(level: str) -> str:
    """Budget scope of a level (``level:N3``), reserved in addition to ``global``."""
    return f"level:{level}"


def extract_key(content_id: int, level: str) -> str:
    return f"ext:{content_id}:{level}"


def stage_key(stage: str, group_id: int, investigation_id: int, round_: int) -> str:
    return f"{stage}:{group_id}:{investigation_id}:{round_}"


def assemble_key(investigation_id: int) -> str:
    return f"asm:{investigation_id}"


def request_key(platform: str, external_id: str) -> str:
    return f"req:{platform}:{external_id}"


def translate_key(verdict_id: int, version: int) -> str:
    return f"tr:{verdict_id}:{version}"


def embed_batch_key(minute_bucket: str) -> str:
    """One embedding batch per UTC minute (``YYYYmmddHHMM``): events of the same
    minute collapse into one job, which then takes every pending content."""
    return f"emb:{minute_bucket}"


def revalidate_key(verdict_id: int) -> str:
    return f"reval:{verdict_id}"


def insee_key(indicator: str, day: str) -> str:
    return f"insee:{indicator}:{day}"


def extract_batch_key(content_ids: list[int], prompt_version: str) -> str:
    import hashlib

    digest = hashlib.sha256(",".join(map(str, sorted(content_ids))).encode()).hexdigest()[:16]
    return f"extb:{digest}:{prompt_version}"


def light_check_key(group_id: int, profile_version: int) -> str:
    return f"light:{group_id}:{profile_version}"


def attach_key(group_id: int) -> str:
    return f"narr:{group_id}"


def title_key(narrative_id: int) -> str:
    return f"title:{narrative_id}"


def promotion_key(group_id: int, hour: str) -> str:
    return f"promo:{group_id}:{hour}"
